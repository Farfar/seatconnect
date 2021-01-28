#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Communicate with Skoda Connect."""
"""Fork of https://github.com/robinostlund/volkswagencarnet where it was modified to support also Skoda Connect"""
"""Modified to utilize Skoda App API instead of Web API"""
import re
import time
import logging
import asyncio
import hashlib
import jwt

from sys import version_info, argv
from datetime import timedelta, datetime
from urllib.parse import urljoin, parse_qs, urlparse
from json import dumps as to_json
import aiohttp
from bs4 import BeautifulSoup
from base64 import b64decode, b64encode
from skodaconnect.utilities import read_config, json_loads
from skodaconnect.vehicle import Vehicle

from aiohttp import ClientSession, ClientTimeout
from aiohttp.hdrs import METH_GET, METH_POST

from .const import (
    HEADERS_SESSION,
    HEADERS_AUTH,
    BASE_SESSION,
    BASE_AUTH,
    CLIENT_ID,
    XCLIENT_ID,
    XAPPVERSION,
    XAPPNAME,
    USER_AGENT,
    APP_URI
)

version_info >= (3, 0) or exit('Python 3 required')

_LOGGER = logging.getLogger(__name__)

TIMEOUT = timedelta(seconds=30)

class Connection:
    """ Connection to Skoda connect """
  # Init connection class
    def __init__(self, session, username, password, fulldebug = False):
        """ Initialize """
        self._session = session
        self._session_fulldebug = fulldebug
        self._session_headers = HEADERS_SESSION.copy()
        self._session_base = BASE_SESSION
        self._session_auth_headers = HEADERS_AUTH.copy()
        self._session_auth_base = BASE_AUTH

        self._session_auth_ref_url = BASE_SESSION
        self._session_spin_ref_url = BASE_SESSION
        self._session_logged_in = False
        self._session_first_update = False
        self._session_auth_username = username
        self._session_auth_password = password
        self._session_tokens = {}

        self._vin = ""
        self._vehicles = []

        _LOGGER.debug('Using service <%s>', self._session_base)

        self._jarCookie = ""
        self._state = {}

    def _clear_cookies(self):
        self._session._cookie_jar._cookies.clear()

  # API Login
    async def _login(self):
        """ Reset session in case we would like to login again """
        self._session_headers = HEADERS_SESSION.copy()
        self._session_auth_headers = HEADERS_AUTH.copy()

        def extract_csrf(req):
            return re.compile('<meta name="_csrf" content="([^"]*)"/>').search(req).group(1)

        def extract_guest_language_id(req):
            return req.split('_')[1].lower()

        def getNonce():
            ts = "%d" % (time.time())
            sha256 = hashlib.sha256()
            sha256.update(ts.encode())
            return (b64encode(sha256.digest()).decode('utf-8')[:-1])

        try:
            # Remove cookies from session as we are doing a new login
            self._clear_cookies()

            # Request landing page and get auth URL:
            req = await self._session.get(
                url='https://identity.vwgroup.io/.well-known/openid-configuration'
            )
            if req.status != 200:
                return ""
            response_data =  await req.json()
            authorizationEndpoint = response_data['authorization_endpoint']
            authissuer = response_data['issuer']

            # Get authorization
            # https://identity.vwgroup.io/oidc/v1/authorize?nonce=yVOPHxDmksgkMo1HDUp6IIeGs9HvWSSWbkhPcxKTGNU&response_type=code id_token token&scope=openid mbb&ui_locales=de&redirect_uri=skodaconnect://oidc.login/&client_id=7f045eee-7003-4379-9968-9355ed2adb06%40apps_vw-dilab_com
            req = await self._session.get(
                url=authorizationEndpoint+\
                    '?redirect_uri='+APP_URI+\
                    '&nonce='+getNonce()+\
                    '&response_type=code id_token token'+\
                    '&client_id='+CLIENT_ID+\
                    '&scope=openid mbb profile address cars email birthdate badge phone driversLicense dealers',
                headers=self._session_auth_headers,
            )
            if req.status != 200:
                return ""
            _LOGGER.debug('Got authorization endpoint, logging on.')
            response_data = await req.text()
            responseSoup = BeautifulSoup(response_data, 'html.parser')
            mailform = dict([(t['name'],t['value']) for t in responseSoup.find('form', id='emailPasswordForm').find_all('input', type='hidden')])
            mailform['email'] = self._session_auth_username
            pe_url = authissuer+responseSoup.find('form', id='emailPasswordForm').get('action')

            # POST email
            # https://identity.vwgroup.io/signin-service/v1/xxx@apps_vw-dilab_com/login/identifier
            self._session_auth_headers['Referer'] = authorizationEndpoint
            self._session_auth_headers['Origin'] = authissuer
            req = await self._session.post(
                url=pe_url,
                headers=self._session_auth_headers,
                data = mailform
            )
            if req.status != 200:
                return ""
            response_data = await req.text()
            responseSoup = BeautifulSoup(response_data, 'html.parser')
            pwform = dict([(t['name'],t['value']) for t in responseSoup.find('form', id='credentialsForm').find_all('input', type='hidden')])
            pwform['password'] = self._session_auth_password
            pw_url = authissuer+responseSoup.find('form', id='credentialsForm').get('action')

            # POST password
            # https://identity.vwgroup.io/signin-service/v1/xxx@apps_vw-dilab_com/login/authenticate
            self._session_auth_headers['Referer'] = pe_url
            self._session_auth_headers['Origin'] = authissuer
            excepted = False

            req = await self._session.post(
                url=pw_url,
                headers=self._session_auth_headers,
                data = pwform,
                allow_redirects=False
            )
            # Follow all redirects until we get redirected back to "our app"
            try:
                maxDepth = 10
                ref = req.headers['Location']
                while not ref.startswith(APP_URI):
                    response = await self._session.get(
                        url=ref,
                        headers=self._session_auth_headers,
                        allow_redirects=False
                    )
                    ref = response.headers['Location']
                    # Set a max limit on requests to prevent forever loop
                    maxDepth -= 1
                    if maxDepth == 0:
                        _LOGGER.warning('Should have gotten a token by now.')
                        return False
            except:
                # If we get excepted it should be because we can't redirect to the skodaconnect:// URL
                _LOGGER.debug('Got code: %s' % ref)
                pass

            # Extract code and tokens
            jwt_auth_code = parse_qs(urlparse(ref).fragment).get('code')[0]
            jwt_access_token = parse_qs(urlparse(ref).fragment).get('access_token')[0]
            jwt_id_token = parse_qs(urlparse(ref).fragment).get('id_token')[0]

            # Exchange Auth code for Skoda tokens
            tokenBody = {
                'auth_code': jwt_auth_code,
                'id_token':  jwt_id_token,
                'brand': 'skoda'
            }
            tokenURL = 'https://tokenrefreshservice.apps.emea.vwapps.io/exchangeAuthCode'
            req = await self._session.post(
                url=tokenURL,
                headers=self._session_auth_headers,
                data = tokenBody,
                allow_redirects=False
            )
            if req.status != 200:
                return ""
            # Save tokens as "identity", this is tokens representing the user
            self._session_tokens['identity'] = await req.json()
            if not await self.verify_tokens(self._session_tokens['identity']['id_token'], 'identity'):
                _LOGGER.warning('User identity token could not be verified!')
            else:
                _LOGGER.debug('User identity token verified OK.')

            # Get VW Group API tokens
            # https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token
            tokenBody2 =  {
                'grant_type': 'id_token',
                'token': self._session_tokens['identity']['id_token'],
                'scope': 'sc2:fal'
            }
            req = await self._session.post(
                    url='https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token',
                    headers= {
                        'User-Agent': USER_AGENT,
                        'X-App-Version': XAPPVERSION,
                        'X-App-Name': XAPPNAME,
                        'X-Client-Id': XCLIENT_ID,
                    },
                    data = tokenBody2,
                    allow_redirects=False
                )
            if req.status > 400:
                _LOGGER.debug('Tokens wrong')
                return ""
            else:
                # Save tokens as "vwg", use theese for get/posts to VW Group API
                self._session_tokens['vwg'] = await req.json()
                if not await self.verify_tokens(self._session_tokens['vwg']['access_token'], 'vwg'):
                    _LOGGER.warning('VW-Group API token could not be verified!')
                else:
                    _LOGGER.debug('VW-Group API token verified OK.')

            # Update headers for requests, defaults to using VWG token
            self._session_headers['Authorization'] = 'Bearer ' + self._session_tokens['vwg']['access_token']
            self._session_logged_in = True

            # Get list of vehicles from account
            _LOGGER.debug('Fetching vehicles associated with account')
            await self.set_token('vwg')
            self._session_headers.pop('Content-Type', None)
            loaded_vehicles = await self.get(
                url='https://msg.volkswagen.de/fs-car/usermanagement/users/v1/skoda/CZ/vehicles'
            )
            # Add all VIN-numbers from account to list of vehicles
            if loaded_vehicles.get('userVehicles', {}).get('vehicle', []):
                _LOGGER.debug('Found vehicle(s) associated with account.')
                for vehicle in loaded_vehicles.get('userVehicles').get('vehicle'):
                    self._vehicles.append(Vehicle(self, vehicle))
                    # Get the Vehicle class object for VIN number and discover initial data
                    await self.vehicle(vehicle).discover()

            return True

        except Exception as error:
            _LOGGER.error('Failed to login to Skoda Connect, %s' % error)
            self._session_logged_in = False
            return False

  # HTTP methods to API
    async def _request(self, method, url, **kwargs):
        """Perform a query to the Skoda Connect service"""
        _LOGGER.debug(f'HTTP {method} "{url}"')
        async with self._session.request(
            method,
            url,
            headers=self._session_headers,
            timeout=ClientTimeout(total=TIMEOUT.seconds),
            cookies=self._jarCookie,
            raise_for_status=False,
            **kwargs
        ) as response:
            response.raise_for_status()

            # Update cookie jar
            if self._jarCookie != '':
                self._jarCookie.update(response.cookies)
            else:
                self._jarCookie = response.cookies

            try:
                if response.status == 204:
                    res = {'status_code': response.status}
                if response.status == 429:
                    _LOGGER.warning(f'Got HTTP 429 "Too many requests" from server. The server has throttled the connection, consider a longer refresh interval.')
                    _LOGGER.debug(f'Headers: {response.headers}')
                elif response.status >= 200 or response.status <= 300:
                    res = await response.json(loads=json_loads)
                else:
                    res = {}
                    _LOGGER.debug(f'Not success status code [{response.status}] response: {response}')
                if 'X-RateLimit-Remaining' in response.headers:
                    res['rate_limit_remaining'] = response.headers.get('X-RateLimit-Remaining', '')
            except:
                res = {}
                _LOGGER.debug(f'Something went wrong [{response.status}] response: {response}')
                return res

            if self._session_fulldebug:
                _LOGGER.debug(f'Request for "{url}" returned with status code [{response.status}], response: {res}')
            else:
                _LOGGER.debug(f'Request for "{url}" returned with status code [{response.status}]')
            return res

    async def get(self, url, vin=''):
        """Perform a get query to the online service."""
        try:
            response = await self._request(METH_GET, self._make_url(url, vin))
            return response
        except aiohttp.client_exceptions.ClientResponseError as error:
            if error.status == 401:
                _LOGGER.warning(f'Received "unauthorized" error while fetching data: {error}')
                self._session_logged_in = False
            elif error.status == 400:
                _LOGGER.error(f'Got HTTP 400 "Bad Request" from server, this request might be malformed or not implemented correctly for this vehicle')
            elif error.status == 500:
                _LOGGER.info('Got HTTP 500 from server, service might be temporarily unavailable')
            elif error.status == 502:
                _LOGGER.info('Got HTTP 502 from server, this request might not be supported for this vehicle')
            else:
                _LOGGER.error(f'Got unhandled error from server: {error.status}')
            return {'status': error.status}

    async def post(self, url, vin='', **data):
        """Perform a post query to the online service."""
        if data:
            return await self._request(METH_POST, self._make_url(url, vin), **data)
        else:
            return await self._request(METH_POST, self._make_url(url, vin))

  # Construct URL from request, home region and variables
    def _make_url(self, ref, vin=''):
        replacedUrl = re.sub('\$vin', vin, ref)
        if ('://' in replacedUrl):
            #already server contained in URL
            return replacedUrl
        elif 'rolesrights' in replacedUrl:
            return urljoin(self._session_spin_ref_url, replacedUrl)
        else:
            return urljoin(self._session_auth_ref_url, replacedUrl)

  # Update data for all Vehicles
    async def update(self):
        """Update status."""
        if self.logged_in == False:
            _LOGGER.debug
            if not await self._login():
                _LOGGER.warning('Login to Skoda Connect failed!')
                return False
        try:
            if not await self.validate_tokens:
                _LOGGER.info('Session has expired. Initiating new login to Skoda Connect.')
                if not await self._login():
                    _LOGGER.warning('Login to Skoda Connect failed!')
                    raise Exception('Login failed')

            _LOGGER.debug('Going to call vehicle updates')
            # Get all Vehicle objects and update in parallell
            updatelist = []
            for vehicle in self.vehicles:
                updatelist.append(vehicle.update())
            # Wait for all data updates to complete
            await asyncio.gather(*updatelist)

            return True
        except (IOError, OSError, LookupError, Exception) as error:
            _LOGGER.warning(f'Could not update information from skoda connect: {error}')
        return False

 #### Data collect functions ####
    async def getHomeRegion(self, vin):
        """Get API requests base url for VIN."""
        try:
            await self.set_token('vwg')
            response = await self.get('https://mal-1a.prd.ece.vwg-connect.com/api/cs/vds/v1/vehicles/$vin/homeRegion', vin)
            self._session_auth_ref_url = response['homeRegion']['baseUri']['content'].split('/api')[0].replace('mal-', 'fal-') if response['homeRegion']['baseUri']['content'] != 'https://mal-1a.prd.ece.vwg-connect.com/api' else 'https://msg.volkswagen.de'
            self._session_spin_ref_url = response['homeRegion']['baseUri']['content'].split('/api')[0]
            return response['homeRegion']['baseUri']['content']
        except Exception as error:
            _LOGGER.debug(f'Could not get homeregion, error {error}')
            self._session_logged_in = False
        return False

    async def getOperationList(self, vin):
        """Collect operationlist for VIN, supported/licensed functions."""
        try:
            await self.set_token('vwg')
            response = await self.get('/api/rolesrights/operationlist/v3/vehicles/$vin', vin)
            if response.get('operationList', False):
                data = response.get('operationList', {})
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch operation list, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Could not fetch operation list: {response}')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch operation list, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getRealCarData(self, vin):
        """Get car information from customer profile, VIN, nickname, etc."""
        try:
            atoken = self._session_tokens['identity']['access_token']
            sub = jwt.decode(atoken, verify=False).get('sub', None)
            await self.set_token('identity')
            response = await self.get(
                'https://customer-profile.apps.emea.vwapps.io/v1/customers/{subject}/realCarData'.format(subject=sub)
            )
            if response.get('realCars', {}):
                data = {
                    'carData': next(item for item in response.get('realCars', []) if item['vehicleIdentificationNumber'] == vin)
                }
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch realCarData, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch realcar data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch realCarData, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getCarportData(self, vin):
        """Get carport data for vehicle, model, model year etc."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/promoter/portfolio/v1/skoda/CZ/vehicle/$vin/carportdata',
                vin = vin
            )
            if response.get('carportData', {}):
                data = {
                    'carportData': response.get('carportData', {})
                }
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch carportdata, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch carport data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch carportData, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getVehicleStatusData(self, vin):
        """Get stored vehicle data response."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/vsr/v1/skoda/CZ/vehicles/$vin/status',
                vin = vin
            )
            if response.get('StoredVehicleDataResponse', {}).get('vehicleData', {}).get('data', {})[0].get('field', {})[0] :
                data = {
                    'StoredVehicleDataResponse': response.get('StoredVehicleDataResponse', {}),
                    'StoredVehicleDataResponseParsed': dict([(e['id'],e if 'value' in e else '') for f in [s['field'] for s in response['StoredVehicleDataResponse']['vehicleData']['data']] for e in f])
                }
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch vehicle status report, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch status data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch StoredVehicleDataResponse, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getTripStatistics(self, vin):
        """Get short term trip statistics."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/tripstatistics/v1/skoda/CZ/vehicles/$vin/tripdata/shortTerm?newest',
                vin = vin
            )
            if response.get('tripData', {}):
                data = {'tripstatistics': response.get('tripData', {})}
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch trip statistics, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info(f'Unhandled error while trying to fetch trip statistics')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch trip statistics, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getPosition(self, vin):
        """Get position data."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/cf/v1/skoda/CZ/vehicles/$vin/position',
                vin = vin
            )
            if response.get('findCarResponse', {}):
                data = {
                    'findCarResponse': response.get('findCarResponse', {}),
                    'isMoving': False
                }
            elif response.get('status_code', {}):
                if response.get('status_code', 0) == 204:
                    _LOGGER.debug(f'Seems car is moving, HTTP 204 received from position')
                    data = {
                        'isMoving': True,
                        'rate_limit_remaining': 15
                    }
                else:
                    _LOGGER.warning(f'Could not fetch position, HTTP status code: {response.get("status_code")}')
                    data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch positional data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch position, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getTimers(self, vin):
        """Get departure timers."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/departuretimer/v1/skoda/CZ/vehicles/$vin/timer',
                vin = vin
            )
            if response.get('timer', {}):
                data = {'timers': response.get('timer', {})}
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch timers, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unknown error while trying to fetch data for departure timers')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch timers, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getClimater(self, vin):
        """Get climatisation data."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/climatisation/v1/skoda/CZ/vehicles/$vin/climater',
                vin = vin
            )
            if response.get('climater', {}):
                data = {'climater': response.get('climater', {})}
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch climatisation, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch climatisation data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch climatisation, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getCharger(self, vin):
        """Get charger data."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/batterycharge/v1/skoda/CZ/vehicles/$vin/charger',
                vin = vin
            )
            if response.get('charger', {}):
                data = {'charger': response.get('charger', {})}
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch pre-heating, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch charger data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch charger, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getPreHeater(self, vin):
        """Get parking heater data."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                'fs-car/bs/rs/v1/skoda/CZ/vehicles/$vin/status',
                vin = vin
            )
            if response.get('statusResponse', {}):
                data = {'heating': response.get('statusResponse', {})}
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch pre-heating, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info('Unhandled error while trying to fetch pre-heating data')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch pre-heating, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def get_request_status(self, vin, sectionId, requestId):
        """Return status of a request ID for a given section ID."""
        if self.logged_in == False:
            if not await self._login():
                _LOGGER.warning('Login to Skoda Connect failed!')
                raise Exception('Login failed')
        try:
            if not await self.validate_tokens:
                _LOGGER.info('Session has expired. Initiating new login to Skoda Connect.')
                if not await self._login():
                    _LOGGER.warning('Login to Skoda Connect failed!')
                    raise Exception('Login failed')
            await self.set_token('vwg')
            if sectionId == 'climatisation':
                url = 'fs-car/bs/$sectionId/v1/Skoda/CZ/vehicles/$vin/climater/actions/$requestId'
            elif sectionId == 'batterycharge':
                url = 'fs-car/bs/$sectionId/v1/Skoda/CZ/vehicles/$vin/charger/actions/$requestId'
            elif sectionId == 'departuretimer':
                url = 'fs-car/bs/$sectionId/v1/Skoda/CZ/vehicles/$vin/timer/actions/$requestId'
            elif sectionId == 'vsr':
                url = "fs-car/bs/$sectionId/v1/Skoda/CZ/vehicles/$vin/requests/$requestId/jobstatus"
            else:
                url = 'fs-car/bs/$sectionId/v1/Skoda/CZ/vehicles/$vin/requests/$requestId/status'
            url = re.sub('\$sectionId', sectionId, url)
            url = re.sub('\$requestId', requestId, url)

            response = await self.get(url, vin)
            # Pre-heater, ???
            if response.get('requestStatusResponse', {}).get('status', False):
                result = response.get('requestStatusResponse', {}).get('status', False)
            # For electric charging, climatisation and departure timers
            elif response.get('action', {}).get('actionState', False):
                result = response.get('action', {}).get('actionState', False)
            else:
                result = 'Unknown'
            # Translate status messages to meaningful info
            if result == 'request_in_progress' or result == 'queued' or result == 'fetched':
                status = 'In progress'
            elif result == 'request_fail' or result == 'failed':
                status = 'Failed'
            elif result == 'unfetched':
                status = 'No response'
            elif result == 'request_successful' or result == 'succeeded':
                status = 'Success'
            else:
                status = result
            return status
        except Exception as error:
            _LOGGER.warning(f'Failure during get request status: {error}')
            raise Exception(f'Failure during get request status: {error}')

    async def get_sec_token(self, vin, spin, action):
        """Get a security token, required for certain set functions."""
        urls = {
            'lock':    '/api/rolesrights/authorization/v2/vehicles/$vin/services/rlu_v1/operations/LOCK/security-pin-auth-requested',
            'unlock':  '/api/rolesrights/authorization/v2/vehicles/$vin/services/rlu_v1/operations/UNLOCK/security-pin-auth-requested',
            'heating': '/api/rolesrights/authorization/v2/vehicles/$vin/services/rheating_v1/operations/P_QSACT/security-pin-auth-requested',
            'timer':   '/api/rolesrights/authorization/v2/vehicles/$vin/services/timerprogramming_v1/operations/P_SETTINGS_AU/security-pin-auth-requested',
            'rclima':  '/api/rolesrights/authorization/v2/vehicles/$vin/services/rclima_v1/operations/P_START_CLIMA_AU/security-pin-auth-requested'
        }
        if not spin:
            raise Exception('SPIN is required')
        try:
            if not urls.get(action, False):
                raise Exception(f'Security token for "{action}" is not implemented')
            response = await self.get(self._make_url(urls.get(action), vin = vin))
            secToken = response['securityPinAuthInfo']['securityToken']
            challenge = response['securityPinAuthInfo']['securityPinTransmission']['challenge']
            spinHash = self.hash_spin(challenge, spin)
            body = {
                'securityPinAuthentication': {
                    'securityPin': {
                        'challenge': challenge,
                        'securityPinHash': spinHash
                    },
                    'securityToken': secToken
                }
            }
            self._session_headers['Content-Type'] = 'application/json'
            response = await self.post(self._make_url('/api/rolesrights/authorization/v2/security-pin-auth-completed', vin = vin), json = body)
            self._session_headers.pop('Content-Type', None)
            if response.get('securityToken', False):
                return response['securityToken']
            else:
                _LOGGER.warning('Did not receive a valid security token')
                raise Exception('Did not receive a valid security token')
        except Exception as error:
            _LOGGER.error(f'Could not generate security token (maybe wrong SPIN?), error: {error}')

 #### Data set functions ####
    async def dataCall(self, query, vin='', **data):
        """Function to execute actions through Skoda Connect servers."""
        if self.logged_in == False:
            if not await self._login():
                _LOGGER.warning('Login to Skoda Connect failed!')
                raise Exception('Login failed')
        try:
            if not await self.validate_tokens:
                _LOGGER.info('Session has expired. Initiating new login to Skoda Connect.')
                if not await self._login():
                    _LOGGER.warning('Login to Skoda Connect failed!')
                    raise Exception('Login failed')
            response = await self.post(query, vin=vin, **data)
            _LOGGER.debug(f'Data call returned: {response}')
            return response
        except aiohttp.client_exceptions.ClientResponseError as error:
            if error.status == 401:
                _LOGGER.error('Unauthorized')
                self._session_logged_in = False
            elif error.status == 400:
                _LOGGER.error(f'Bad request')
            elif error.status == 500:
                _LOGGER.error('Internal server error, server might be temporarily unavailable')
            elif error.status == 502:
                _LOGGER.error('Bad gateway, this function may not be implemented for this vehicle')
            else:
                _LOGGER.error(f'Unhandled HTTP exception: {error}')
            return False
        except Exception as error:
            _LOGGER.error(f'Failure to execute: {error}')
            return False

    async def setRefresh(self, vin):
        """"Force vehicle data update."""
        try:
            await self.set_token('vwg')
            response = await self.dataCall('fs-car/bs/vsr/v1/skoda/CZ/vehicles/$vin/requests', vin, data=None)
            if not response:
                _LOGGER.warning(f'Failed to execute data refresh')
                raise Exception(f'Failed to execute data refresh')
            else:
                request_id = response.get('CurrentVehicleDataResponse', {}).get('requestId', 0)
                request_state = response.get('CurrentVehicleDataResponse', {}).get('requestState', 'queued')
                remaining = response.get('rate_limit_remaining', -1)
                _LOGGER.debug(f'Request to refresh data returned with state "{request_state}", request id: {request_id}, remaining requests: {remaining}')
                return dict({'id': str(request_id), 'state': request_state, 'rate_limit_remaining': remaining})
        except Exception as error:
            _LOGGER.warning(f'Data refresh failed - {error}')
            raise Exception(f'Data refresh failed - {error}')

    async def setCharger(self, vin, data):
        """Start/Stop charger."""
        try:
            await self.set_token('vwg')
            response = await self.dataCall('fs-car/bs/batterycharge/v1/Skoda/CZ/vehicles/$vin/charger/actions', vin, json = data)
            if not response:
                _LOGGER.warning('Failed to execute charger action.')
                return False
            else:
                request_id = response.get('action', {}).get('actionId', 0)
                request_state = response.get('action', {}).get('actionState', 'unknown')
                remaining = response.get('rate_limit_remaining', -1)
                _LOGGER.debug(f'Request for charger action returned with state "{request_state}", request id: {request_id}, remaining requests: {remaining}')
                return dict({'id': str(request_id), 'state': request_state, 'rate_limit_remaining': remaining})
        except Exception as error:
            _LOGGER.warning(f'Failed to execute charger action - {error}')
        return False

    async def setClimater(self, vin, data, spin):
        """Execute climatisation actions."""
        try:
            await self.set_token('vwg')
            # Only get security token if auxiliary heater is to be started
            if data.get('action', {}).get('settings', {}).get('heaterSource', None) == 'auxiliary':
                self._session_headers['X-securityToken'] = await self.get_sec_token(vin = vin, spin = spin, action = 'rclima')
            response = await self.dataCall('fs-car/bs/climatisation/v1/Skoda/CZ/vehicles/$vin/climater/actions', vin, json = data)
            self._session_headers.pop('X-securityToken', None)
            if not response:
                raise Exception(f'Invalid response: "{response}"')
            else:
                request_id = response.get('action', {}).get('actionId', 0)
                request_state = response.get('action', {}).get('actionState', 'unknown')
                remaining = response.get('rate_limit_remaining', -1)
                _LOGGER.debug(f'Request for climater action returned with state "{request_state}", request id: {request_id}, remaining requests: {remaining}')
                return dict({'id': str(request_id), 'state': request_state, 'rate_limit_remaining': remaining})
        except Exception as error:
            self._session_headers.pop('X-securityToken', None)
            #_LOGGER.warning(f'Failed to execute charger action - {error}')
            raise
        return False

    async def setPreHeater(self, vin, data, spin):
        """Petrol/diesel parking heater actions."""
        try:
            await self.set_token('vwg')
            if 'Content-Type' in self._session_headers:
                contType = self._session_headers['Content-Type']
            else:
                contType = ''
            self._session_headers['Content-Type'] = 'application/vnd.vwg.mbb.RemoteStandheizung_v2_0_2+json'
            self._session_headers['x-mbbSecToken'] = await self.get_sec_token(vin = vin, spin = spin, action = 'heating')
            response = await self.dataCall('fs-car/bs/rs/v1/skoda/CZ/vehicles/$vin/action', vin = vin, json = data)
            # Clean up headers
            self._session_headers.pop('x-mbbSecToken', None)
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType

            if not response:
                raise Exception(f'Invalid response: "{response}"')
            else:
                request_id = response.get('performActionResponse', {}).get('requestId', 0)
                request_state = response.get('performActionResponse', {}).get('requestState', 'unknown')
                remaining = response.get('rate_limit_remaining', -1)
                _LOGGER.debug(f'Request for parking heater returned with state "{request_state}", request id: {request_id}, remaining requests: {remaining}')
                return dict({'id': str(request_id), 'state': request_state, 'rate_limit_remaining': remaining})
        except Exception as error:
            self._session_headers.pop('x-mbbSecToken', None)
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType
            raise
        return False

    async def setLock(self, vin, data, spin):
        """Remote lock and unlock actions."""
        try:
            await self.set_token('vwg')
            # Prepare data, headers and fetch security token
            if 'Content-Type' in self._session_headers:
                contType = self._session_headers['Content-Type']
            else:
                contType = ''
            self._session_headers['X-mbbSecToken'] = await self.get_sec_token(vin = vin, spin = spin, action = 'lock')
            self._session_headers['Content-Type'] = 'application/vnd.vwg.mbb.RemoteLockUnlock_v1_0_0+xml'
            response = await self.dataCall('fs-car/bs/rlu/v1/skoda/CZ/vehicles/$vin/actions', vin, data = data)
            # Clean up headers
            self._session_headers.pop('X-mbbSecToken', None)
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType
            if not response:
                _LOGGER.warning('Failed to execute lock action.')
                return False
            else:
                request_id = response.get('rluActionResponse', {}).get('requestId', 0)
                request_state = response.get('rluActionResponse', {}).get('requestId', 'unknown')
                remaining = response.get('rate_limit_remaining', -1)
                _LOGGER.debug(f'Request for lock action returned with state "{request_state}", request id: {request_id}, remaining requests: {remaining}')
                return dict({'id': str(request_id), 'state': request_state, 'rate_limit_remaining': remaining})
        except Exception as error:
            _LOGGER.error(f'Failed to execute lock action - {error}')
        self._session_headers.pop('X-mbbSecToken', None)
        self._session_headers.pop('Content-Type', None)
        if contType: self._session_headers['Content-Type'] = contType
        return False

 #### Token handling ####
    @property
    async def validate_tokens(self):
        """Function to validate expiry of tokens."""
        idtoken = self._session_tokens['identity']['id_token']
        atoken = self._session_tokens['vwg']['access_token']
        id_exp = jwt.decode(idtoken, verify=False).get('exp', None)
        at_exp = jwt.decode(atoken, verify=False).get('exp', None)
        id_dt = datetime.fromtimestamp(int(id_exp))
        at_dt = datetime.fromtimestamp(int(at_exp))
        now = datetime.now()
        # We check if the tokens expire in the next minute
        later = now + timedelta(minutes=1)
        if now >= id_dt or now >= at_dt:
            _LOGGER.debug('Tokens have expired. Try to fetch new tokens.')
            if await self.refresh_tokens():
                _LOGGER.debug('Successfully refreshed tokens')
            else:
                return False
        elif later >= id_dt or later >= at_dt:
            _LOGGER.debug('Tokens about to expire. Try to fetch new tokens.')
            if await self.refresh_tokens():
                _LOGGER.debug('Successfully refreshed tokens')
            else:
                return False
        return True

    async def verify_tokens(self, token, type):
        """Function to verify JWT against JWK(s)."""
        if type == 'identity':
            req = await self._session.get(url = 'https://identity.vwgroup.io/oidc/v1/keys')
            keys = await req.json()
            audience = [
                CLIENT_ID,
                'VWGMBB01DELIV1',
                'https://api.vas.eu.dp15.vwg-connect.com',
                'https://api.vas.eu.wcardp.io'
            ]
        elif type == 'vwg':
            req = await self._session.get(url = 'https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/public/jwk/v1')
            keys = await req.json()
            audience = 'mal.prd.ece.vwg-connect.com'
        else:
            _LOGGER.debug('Not implemented')
            return False
        try:
            pubkeys = {}
            for jwk in keys['keys']:
                kid = jwk['kid']
                if jwk['kty'] == 'RSA':
                    pubkeys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(to_json(jwk))

            token_kid = jwt.get_unverified_header(token)['kid']
            if type == 'vwg':
                token_kid = 'VWGMBB01DELIV1.' + token_kid

            pubkey = pubkeys[token_kid]
            payload = jwt.decode(token, key=pubkey, algorithms=['RS256'], audience=audience)
            return True
        except Exception as error:
            _LOGGER.debug(f'Failed to verify token, error: {error}')
            return False

    async def refresh_tokens(self):
        """Function to refresh tokens."""
        try:
            tHeaders = {
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': USER_AGENT,
                'X-App-Version': XAPPVERSION,
                'X-App-Name': XAPPNAME,
                'X-Client-Id': XCLIENT_ID
            }

            body = {
                'grant_type': 'refresh_token',
                'brand': 'Skoda',
                'refresh_token': self._session_tokens['identity']['refresh_token']
            }
            response = await self._session.post(
                url = 'https://tokenrefreshservice.apps.emea.vwapps.io/refreshTokens',
                headers = tHeaders,
                data = body
            )
            if response.status == 200:
                tokens = await response.json()
                # Verify Token
                if not await self.verify_tokens(tokens['id_token'], 'identity'):
                    _LOGGER.warning('Token could not be verified!')
                for token in tokens:
                    self._session_tokens['identity'][token] = tokens[token]
            else:
                _LOGGER.warning('Something went wrong when refreshing Skoda tokens.')
                return False

            body = {
                'grant_type': 'id_token',
                'scope': 'sc2:fal',
                'token': self._session_tokens['identity']['id_token']
            }

            response = await self._session.post(
                url = 'https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token',
                headers = tHeaders,
                data = body,
                allow_redirects=True
            )
            if response.status == 200:
                tokens = await response.json()
                if not await self.verify_tokens(tokens['access_token'], 'vwg'):
                    _LOGGER.warning('Token could not be verified!')
                for token in tokens:
                    self._session_tokens['vwg'][token] = tokens[token]
            else:
                resp = await response.text()
                _LOGGER.warning('Something went wrong when refreshing API tokens. %s' % resp)
                return False
            return True
        except Exception as error:
            _LOGGER.warning(f'Could not refresh tokens: {error}')
            return False

    async def set_token(self, type):
        """Switch between tokens."""
        self._session_headers['Authorization'] = 'Bearer ' + self._session_tokens[type]['access_token']
        return

 #### Class helpers ####
    @property
    def vehicles(self):
        """Return list of Vehicle objects."""
        return self._vehicles

    @property
    def logged_in(self):
        return self._session_logged_in

    def vehicle(self, vin):
        """Return vehicle object for given vin."""
        return next(
            (
                vehicle
                for vehicle in self.vehicles
                if vehicle.unique_id.lower() == vin.lower()
            ), None
        )

    def hash_spin(self, challenge, spin):
        """Convert SPIN and challenge to hash."""
        spinArray = bytearray.fromhex(spin);
        byteChallenge = bytearray.fromhex(challenge);
        spinArray.extend(byteChallenge)
        return hashlib.sha512(spinArray).hexdigest()

async def main():
    """Main method."""
    if '-v' in argv:
        logging.basicConfig(level=logging.INFO)
    elif '-vv' in argv:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.ERROR)

    async with ClientSession(headers={'Connection': 'keep-alive'}) as session:
        connection = Connection(session, **read_config())
        if await connection._login():
            if await connection.update():
                for vehicle in connection.vehicles:
                    print(f'Vehicle id: {vehicle}')
                    print('Supported sensors:')
                    for instrument in vehicle.dashboard().instruments:
                        print(f' - {instrument.name} (domain:{instrument.component}) - {instrument.str_state}')

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
