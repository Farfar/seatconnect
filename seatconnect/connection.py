#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Communicate with Seat Connect."""
"""First fork from https://github.com/robinostlund/volkswagencarnet where it was modified to support also Skoda Connect"""
"""Then forked from https://github.com/lendy007/skodaconnect for adaptation to Seat Connect"""
import re
import time
import logging
import asyncio
import hashlib
import jwt
import hmac
import xmltodict

from sys import version_info, argv
from datetime import timedelta, datetime, timezone
from urllib.parse import urljoin, parse_qs, urlparse, urlencode
from json import dumps as to_json
from jwt.exceptions import ExpiredSignatureError
import aiohttp
from bs4 import BeautifulSoup
from base64 import b64decode, b64encode
from seatconnect.__version__ import __version__ as lib_version
from seatconnect.utilities import read_config, json_loads
from seatconnect.vehicle import Vehicle
from seatconnect.exceptions import (
    SeatConfigException,
    SeatAuthenticationException,
    SeatAccountLockedException,
    SeatTokenExpiredException,
    SeatException,
    SeatEULAException,
    SeatThrottledException,
    SeatLoginFailedException,
    SeatInvalidRequestException,
    SeatRequestInProgressException,
    SeatServiceUnavailable
)

from aiohttp import ClientSession, ClientTimeout
from aiohttp.hdrs import METH_GET, METH_POST

from .const import (
    BRAND,
    COUNTRY,
    HEADERS_SESSION,
    HEADERS_AUTH,
    TOKEN_HEADERS,
    BASE_SESSION,
    BASE_AUTH,
    CLIENT_LIST,
    XCLIENT_ID,
    XAPPVERSION,
    XAPPNAME,
    USER_AGENT,
    APP_URI,
    MODELVIEW,
    MODELAPPID,
    MODELAPIKEY,
    MODELHOST,
    MODELAPI
)

version_info >= (3, 0) or exit('Python 3 required')

_LOGGER = logging.getLogger(__name__)

TIMEOUT = timedelta(seconds=30)

class Connection:
    """ Connection to Seat connect """
  # Init connection class
    def __init__(self, session, username, password, fulldebug=False, interval=timedelta(minutes=5)):
        """ Initialize """
        self._session = session
        self._lock = asyncio.Lock()
        self._session_fulldebug = fulldebug
        self._session_headers = HEADERS_SESSION.copy()
        self._session_base = BASE_SESSION
        self._session_auth_headers = HEADERS_AUTH.copy()
        self._session_cookies = ""
        self._session_nonce = self._getNonce()
        self._session_state = self._getState()

        self._session_auth_ref_url = BASE_SESSION
        self._session_spin_ref_url = BASE_SESSION
        self._session_first_update = False
        self._session_auth_username = username
        self._session_auth_password = password
        self._session_tokens = {}

        self._vehicles = []

        _LOGGER.info(f'Init Seat Connect library, version {lib_version}')
        _LOGGER.debug(f'Using service {self._session_base}')


    def _clear_cookies(self):
        self._session._cookie_jar._cookies.clear()
        self._session_cookies = ''

    def _getNonce(self):
        ts = "%d" % (time.time())
        sha256 = hashlib.sha256()
        sha256.update(ts.encode())
        return (b64encode(sha256.digest()).decode('utf-8')[:-1])

    def _getState(self):
        ts = "%d" % (time.time()-1000)
        sha256 = hashlib.sha256()
        sha256.update(ts.encode())
        return (b64encode(sha256.digest()).decode('utf-8')[:-1])

  # API login
    async def doLogin(self):
        """Login method, clean login"""
        _LOGGER.info('Initiating new login')

        if len(self._session_tokens) > 0:
            _LOGGER.info('Revoking old tokens.')
            try:
                await self.logout()
            except:
                pass

        # Remove cookies and re-init session
        self._clear_cookies()
        self._vehicles.clear()
        self._session_tokens = {}
        self._session_headers = HEADERS_SESSION.copy()
        self._session_auth_headers = HEADERS_AUTH.copy()
        self._session_nonce = self._getNonce()
        self._session_state = self._getState()

        # Login with Seat client
        return await self._authorize(BRAND)

    async def _authorize(self, client=BRAND):
        """"Login" function. Authorize a certain client type and get tokens."""
        # Helper functions
        def extract_csrf(req):
            return re.compile('<meta name="_csrf" content="([^"]*)"/>').search(req).group(1)

        def extract_guest_language_id(req):
            return req.split('_')[1].lower()

        # Login/Authorization starts here
        try:
            self._session_headers = HEADERS_SESSION.copy()
            self._session_auth_headers = HEADERS_AUTH.copy()

            _LOGGER.debug(f'Starting authorization process for client {client}')
            req = await self._session.get(
                url='https://identity.vwgroup.io/.well-known/openid-configuration'
            )
            if req.status != 200:
                return False
            response_data =  await req.json()
            authorizationEndpoint = response_data['authorization_endpoint']
            authissuer = response_data['issuer']

            # Get authorization page (login page)
            if self._session_fulldebug:
                _LOGGER.debug(f'Get authorization page: "{authorizationEndpoint}"')
            try:
                req = await self._session.get(
                    url=authorizationEndpoint+\
                        '?redirect_uri='+APP_URI+\
                            '&nonce='+self._session_nonce+\
                            '&state='+self._session_state+\
                            '&response_type='+CLIENT_LIST[client].get('TOKEN_TYPES')+\
                            '&client_id='+CLIENT_LIST[client].get('CLIENT_ID')+\
                            '&scope='+CLIENT_LIST[client].get('SCOPE'),
                        headers=self._session_auth_headers,
                        allow_redirects=False
                    )
                if req.headers.get('Location', False):
                    ref = req.headers.get('Location', '')
                    if 'error' in ref:
                        error = parse_qs(urlparse(ref).query).get('error', '')[0]
                        if 'error_description' in ref:
                            error = parse_qs(urlparse(ref).query).get('error_description', '')[0]
                            _LOGGER.info(f'Unable to login, {error}')
                        else:
                            _LOGGER.info(f'Unable to login.')
                        raise SeatException(error)
                    else:
                        if self._session_fulldebug:
                            _LOGGER.debug(f'Got authorization endpoint: "{ref}"')
                        req = await self._session.get(
                            url=ref,
                            headers=self._session_auth_headers,
                            allow_redirects=False
                        )
                else:
                    _LOGGER.warning(f'Unable to fetch authorization endpoint')
                    raise SeatException('Missing "location" header')
            except (SeatException):
                raise
            except Exception as error:
                _LOGGER.warning(f'Failed to get authorization endpoint. {error}')
                raise SeatException(error)

            # If we need to sign in (first token)
            if 'signin-service' in ref:
                _LOGGER.debug("Got redirect to signin-service")
                location = await self._signin_service(req, authissuer, authorizationEndpoint)
            else:
                # We are already logged on, shorter authorization flow
                location = req.headers.get('Location', None)

            # Follow all redirects until we reach the callback URL
            try:
                maxDepth = 10
                while not location.startswith(APP_URI):
                    if location is None:
                        raise SeatException('Login failed')
                    if 'error' in location:
                        error = parse_qs(urlparse(location).query).get('error', '')[0]
                        if error == 'login.error.throttled':
                            timeout = parse_qs(urlparse(location).query).get('enableNextButtonAfterSeconds', '')[0]
                            raise SeatAccountLockedException(f'Account is locked for another {timeout} seconds')
                        elif error == 'login.errors.password_invalid':
                            raise SeatAuthenticationException('Invalid credentials')
                        else:
                            _LOGGER.warning(f'Login failed: {error}')
                        raise SeatLoginFailedException(error)
                    if 'terms-and-conditions' in location:
                        raise SeatEULAException('The terms and conditions must be accepted first at "https://my.seat/portal/"')
                    if self._session_fulldebug:
                        _LOGGER.debug(f'Following redirect to "{location}"')
                    response = await self._session.get(
                        url=location,
                        headers=self._session_auth_headers,
                        allow_redirects=False
                    )
                    if response.headers.get('Location', False) is False:
                        _LOGGER.debug(f'Unexpected response: {await req.text()}')
                        raise SeatAuthenticationException('User appears unauthorized')
                    location = response.headers.get('Location', None)
                    # Set a max limit on requests to prevent forever loop
                    maxDepth -= 1
                    if maxDepth == 0:
                        raise SeatException('Too many redirects')
            except (SeatException, SeatEULAException, SeatAuthenticationException, SeatAccountLockedException, SeatLoginFailedException):
                raise
            except Exception as e:
                # If we get an unhandled exception it should be because we can't redirect to the APP_URI URL and thus we have our auth code
                if 'code' in location:
                    if self._session_fulldebug:
                        _LOGGER.debug('Got code: %s' % location)
                    pass
                else:
                    _LOGGER.debug(f'Exception occured while logging in.')
                    raise SeatLoginFailedException(e)

            _LOGGER.debug('Received authorization code, exchange for tokens.')
            # Extract code and tokens
            jwt_auth_code = parse_qs(urlparse(location).fragment).get('code')[0]
            jwt_id_token = parse_qs(urlparse(location).fragment).get('id_token')[0]
            tokenBody = {
                'auth_code': jwt_auth_code,
                'id_token':  jwt_id_token,
                'brand': BRAND
            }
            tokenURL = 'https://tokenrefreshservice.apps.emea.vwapps.io/exchangeAuthCode'
            req = await self._session.post(
                url=tokenURL,
                headers=self._session_auth_headers,
                data = tokenBody,
                allow_redirects=False
            )
            if req.status != 200:
                if req.status >= 500:
                    raise SeatServiceUnavailable(f'API returned HTTP status {req.status}')
                raise SeatException(f'Token exchange failed. Request status: {req.status}')
            # Save access, identity and refresh tokens according to requested client
            token_data = await req.json()
            self._session_tokens[client] = {}
            for key in token_data:
                if '_token' in key:
                    self._session_tokens[client][key] = token_data[key]
            _LOGGER.debug(f'Token data: {token_data}')
            if 'error' in self._session_tokens[client]:
                error = self._session_tokens[client].get('error', '')
                if 'error_description' in self._session_tokens[client]:
                    error_description = self._session_tokens[client].get('error_description', '')
                    raise SeatException(f'{error} - {error_description}')
                else:
                    raise SeatException(error)
            if self._session_fulldebug:
                for key in self._session_tokens.get(client, {}):
                    if 'token' in key:
                        _LOGGER.debug(f'Got {key} for client {CLIENT_LIST[client].get("CLIENT_ID","")}, token: "{self._session_tokens.get(client, {}).get(key, None)}"')
            # Verify token, warn if problems are found
            verify = await self.verify_token(self._session_tokens[client].get('id_token', ''))
            if verify is False:
                _LOGGER.warning(f'Token for {client} is invalid!')
            elif verify is True:
                _LOGGER.debug(f'Token for {client} verified OK.')
            else:
                _LOGGER.warning(f'Token for {client} could not be verified, verification returned {verify}.')
        except (SeatEULAException):
            _LOGGER.warning('Login failed, the terms and conditions might have been updated and need to be accepted. Login to https://my.seat/portal/ and accept the new terms before trying again')
            raise
        except (SeatAccountLockedException):
            _LOGGER.warning('Your account is locked, probably because of too many incorrect login attempts. Make sure that your account is not in use somewhere with incorrect password')
            raise
        except (SeatAuthenticationException):
            _LOGGER.warning('Invalid credentials or invalid configuration. Make sure you have entered the correct credentials')
            raise
        except (SeatException):
            _LOGGER.error('An API error was encountered during login, try again later')
            raise
        except (TypeError):
            _LOGGER.warning(f'Login failed for {self._session_auth_username}. The server might be temporarily unavailable, try again later. If the problem persists, verify your account at https://my.seat/portal/')
        except Exception as error:
            _LOGGER.error(f'Login failed for {self._session_auth_username}, {error}')
            return False
        return True

    async def _signin_service(self, html, authissuer, authorizationEndpoint):
        """Method for signin to connect portal."""
        # Extract login form and extract attributes
        try:
            response_data = await html.text()
            responseSoup = BeautifulSoup(response_data, 'html.parser')
            mailform = dict()
            if responseSoup is None:
                raise SeatLoginFailedException('Login failed, server did not return a login form')
            for t in responseSoup.find('form', id='emailPasswordForm').find_all('input', type='hidden'):
                if self._session_fulldebug:
                    _LOGGER.debug(f'Extracted form attribute: {t["name"], t["value"]}')
                mailform[t['name']] = t['value']
            #mailform = dict([(t['name'],t['value']) for t in responseSoup.find('form', id='emailPasswordForm').find_all('input', type='hidden')])
            mailform['email'] = self._session_auth_username
            pe_url = authissuer+responseSoup.find('form', id='emailPasswordForm').get('action')
        except Exception as e:
            _LOGGER.error('Failed to extract user login form.')
            raise

        # POST email
        # https://identity.vwgroup.io/signin-service/v1/{CLIENT_ID}/login/identifier
        self._session_auth_headers['Referer'] = authorizationEndpoint
        self._session_auth_headers['Origin'] = authissuer
        _LOGGER.debug(f"Start authorization for user {self._session_auth_username}")
        req = await self._session.post(
            url = pe_url,
            headers = self._session_auth_headers,
            data = mailform
        )
        if req.status != 200:
            raise SeatException('Authorization request failed')
        try:
            response_data = await req.text()
            responseSoup = BeautifulSoup(response_data, 'html.parser')
            pwform = {}
            for t in responseSoup.find('form', id='credentialsForm').find_all('input', type='hidden'):
                if self._session_fulldebug:
                    _LOGGER.debug(f'Extracted form attribute: {t["name"], t["value"]}')
                pwform[t['name']] = t['value']
            #pwform = dict([(t['name'],t['value']) for t in responseSoup.find('form', id='credentialsForm').find_all('input', type='hidden')])
            pwform['password'] = self._session_auth_password
            pw_url = authissuer+responseSoup.find('form', id='credentialsForm').get('action')
        except Exception as e:
            if responseSoup.find('form', id='credentialsForm') is None:
                raise SeatAuthenticationException("Invalid username")
            raise SeatAuthenticationException("Invalid username or service unavailable")

        # POST password
        # https://identity.vwgroup.io/signin-service/v1/{CLIENT_ID}/login/authenticate
        self._session_auth_headers['Referer'] = pe_url
        self._session_auth_headers['Origin'] = authissuer
        _LOGGER.debug(f"Finalizing login")
        if self._session_fulldebug:
            _LOGGER.debug(f'Using login action url: "{pw_url}"')
        req = await self._session.post(
            url=pw_url,
            headers=self._session_auth_headers,
            data = pwform,
            allow_redirects=False
        )
        return req.headers.get('Location', None)

    async def _getAPITokens(self):
        """Method to acquire VW-Group API tokens."""
        try:
            # Check for valid token
            token = self._session_tokens.get(BRAND, {}).get('id_token', None)
            if token is None:
                _LOGGER.debug('Token is missing, call to authorize the client.')
                if await self._authorize(BRAND) is True:
                    token = self._session_tokens.get(BRAND, {}).get('id_token', None)
                else:
                    raise SeatAuthenticationException('Failed to authorize client "connect"')

            # If connect token is not valid, try to refresh it
            if not await self.validate_token(token):
                # Try to refresh "Connect" token
                if not await refresh_token(BRAND):
                    raise SeatTokenExpiredException('Token is invalid for client')

            # https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token
            tokenBody2 =  {
                'token': self._session_tokens[BRAND]['id_token'],
                'grant_type': 'id_token',
                'scope': 'sc2:fal'
            }
            _LOGGER.debug('Trying to fetch api tokens.')
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
                _LOGGER.debug('API token request failed.')
                raise SeatException(f'API token request returned with status code {req.status}')
            else:
                # Save tokens as "vwg", use theese for get/posts to VW Group API
                token_data = await req.json()
                self._session_tokens['vwg'] = {}
                for key in token_data:
                    if '_token' in key:
                        self._session_tokens['vwg'][key] = token_data[key]
                if 'error' in self._session_tokens['vwg']:
                    error = self._session_tokens['vwg'].get('error', '')
                    if 'error_description' in self._session_tokens['vwg']:
                        error_description = self._session_tokens['vwg'].get('error_description', '')
                        raise SeatException(f'{error} - {error_description}')
                    else:
                        raise SeatException(error)
                if not await self.verify_token(self._session_tokens['vwg'].get('access_token', '')):
                    _LOGGER.warning('VW-Group API token could not be verified!')
                else:
                    _LOGGER.debug('VW-Group API token verified OK.')

            # Update headers for requests, defaults to using VWG token
            self._session_headers['Authorization'] = 'Bearer ' + self._session_tokens['vwg']['access_token']
        #except Exception as error:
        #    _LOGGER.error(f'Failed to fetch VW-Group API tokens, {error}')
        #    return False
        except:
            raise
        return True

    async def terminate(self):
        """Log out from connect services"""
        _LOGGER.info(f'Initiating logout')
        await self.logout()

    async def logout(self):
        """Logout, revoke tokens."""
        self._session_headers.pop('Authorization', None)
        self._session_headers.pop('tokentype', None)
        self._session_headers['Content-Type'] = 'application/x-www-form-urlencoded'

        for client in self._session_tokens:
            # Ignore identity tokens
            for token_type in (
                token_type
                for token_type in self._session_tokens[client]
                if token_type in ['refresh_token', 'access_token']
            ):
                # VW-Group tokens need their own data and url
                if client == 'vwg':
                    params = {
                        'token': self._session_tokens[client][token_type],
                        'token_type_hint': token_type
                    }
                    revoke_url = 'https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/revoke'
                else:
                    params = {
                        "token": self._session_tokens[client][token_type],
                        "brand": BRAND
                    }
                    revoke_url = 'https://tokenrefreshservice.apps.emea.vwapps.io/revokeToken'

                # Only VW-Group access_token is revokeable
                if not client == 'vwg' and token_type == 'access_token':
                    pass
                # Revoke tokens
                else:
                    try:
                        if await self.post(revoke_url, data = params):
                            _LOGGER.info(f'Revocation of "{token_type}" for client "{client}" successful')
                            # Remove token info
                            self._session_tokens[client][token_type] = None
                        else:
                            _LOGGER.warning(f'Revocation of "{token_type}" for client "{client}" failed')
                    except Exception as e:
                        _LOGGER.info(f'Revocation failed with error: {e}')
                        pass

  # HTTP methods to API
    async def get(self, url, vin=''):
        """Perform a HTTP GET."""
        try:
            response = await self._request(METH_GET, url)
            return response
        except aiohttp.client_exceptions.ClientResponseError as error:
            if error.status == 401:
                _LOGGER.warning(f'Received "unauthorized" error while fetching data: {error}')
            elif error.status == 400:
                _LOGGER.error(f'Got HTTP 400 {error}"Bad Request" from server, this request might be malformed or not implemented correctly for this vehicle')
            elif error.status == 500:
                _LOGGER.info('Got HTTP 500 from server, service might be temporarily unavailable')
            elif error.status == 502:
                _LOGGER.info('Got HTTP 502 from server, this request might not be supported for this vehicle')
            else:
                _LOGGER.error(f'Got unhandled error from server: {error.status}')
            return {'status_code': error.status}
        except Exception as e:
            _LOGGER.debug(f'Got non HTTP related error: {e}')

    async def post(self, url, **data):
        """Perform a HTTP POST."""
        if data:
            return await self._request(METH_POST, url, **data)
        else:
            return await self._request(METH_POST, url)

    async def _request(self, method, url, **kwargs):
        """Perform a HTTP query"""
        _LOGGER.debug(f'HTTP {method} "{url}"')
        async with self._session.request(
            method,
            url,
            headers=self._session_headers,
            timeout=ClientTimeout(total=TIMEOUT.seconds),
            cookies=self._session_cookies,
            raise_for_status=False,
            **kwargs
        ) as response:
            response.raise_for_status()

            # Update cookie jar
            if self._session_cookies != '':
                self._session_cookies.update(response.cookies)
            else:
                self._session_cookies = response.cookies

            try:
                if response.status == 204:
                    res = {'status_code': response.status}
                elif response.status >= 200 or response.status <= 300:
                    # If this is a revoke token url, expect Content-Length 0 and return
                    if int(response.headers.get('Content-Length', 0)) == 0 and 'revoke' in url:
                        if response.status == 200:
                            return True
                        else:
                            return False
                    else:
                        if 'xml' in response.headers.get('Content-Type', ''):
                            res = xmltodict.parse(await response.text())
                            #res = to_json(obj)
                        else:
                            res = await response.json(loads=json_loads)
                else:
                    res = {}
                    _LOGGER.debug(f'Not success status code [{response.status}] response: {response}')
                if 'X-RateLimit-Remaining' in response.headers:
                    res['rate_limit_remaining'] = response.headers.get('X-RateLimit-Remaining', '')
            except Exception as e:
                res = {}
                _LOGGER.debug(f'Something went wrong [{response.status}] response: {response}, error: {e}')
                return res

            if self._session_fulldebug:
                _LOGGER.debug(f'Request for "{url}" returned with status code [{response.status}], response: {res}')
            else:
                _LOGGER.debug(f'Request for "{url}" returned with status code [{response.status}]')
            return res

    async def _data_call(self, query, **data):
        """Function for POST actions with error handling."""
        try:
            response = await self.post(query, **data)
            _LOGGER.debug(f'Data call returned: {response}')
            return response
        except aiohttp.client_exceptions.ClientResponseError as error:
            if error.status == 401:
                _LOGGER.error('Unauthorized')
            elif error.status == 400:
                _LOGGER.error(f'Bad request')
            elif error.status == 429:
                _LOGGER.warning('Too many requests. Further requests can only be made after the end of next trip in order to protect your vehicles battery.')
                return 429
            elif error.status == 500:
                _LOGGER.error('Internal server error, server might be temporarily unavailable')
            elif error.status == 502:
                _LOGGER.error('Bad gateway, this function may not be implemented for this vehicle')
            else:
                _LOGGER.error(f'Unhandled HTTP exception: {error}')
            #return False
        except Exception as error:
            _LOGGER.error(f'Failure to execute: {error}')
        return False

  # Class get data functions
    async def update_all(self):
        """Update status."""
        try:
            await self.set_token('vwg')
            # Get all Vehicle objects and update in parallell
            update_list = []
            for vehicle in self.vehicles:
                if vehicle.vin not in update_list:
                    _LOGGER.debug(f'Adding {vehicle.vin} for data refresh')
                    update_list.append(vehicle.update())
                else:
                    _LOGGER.debug(f'VIN {vehicle.vin} is already queued for data refresh')

            # Wait for all data updates to complete
            if len(update_list) == 0:
                _LOGGER.info('No vehicles in account to update')
            else:
                _LOGGER.debug('Calling update function for all vehicles')
                await asyncio.gather(*update_list)
            return True
        except (IOError, OSError, LookupError, Exception) as error:
            _LOGGER.warning(f'An error was encountered during interaction with the API: {error}')
        except:
            raise
        return False

    async def get_vehicles(self):
        """Fetch vehicle information from user profile."""
        api_vehicles = []
        # Check if user needs to update consent
        try:
            await self.set_token(BRAND)
            consent = await self.getConsentInfo()
            if isinstance(consent, dict):
                _LOGGER.debug(f'Consent returned {consent}')
                if 'status' in consent.get('mandatoryConsentInfo', []):
                    if consent.get('mandatoryConsentInfo', [])['status'] != 'VALID':
                        raise SeatEULAException(f'User needs to update consent for {consent.get("mandatoryConsentInfo", [])["id"]}')
                elif len(consent.get('missingMandatoryFields', [])) > 0:
                    raise SeatEULAException(f'Missing mandatory field for user: {consent.get("missingMandatoryFields", [])[0].get("name", "")}')
                else:
                    _LOGGER.debug('User consent is valid, no missing information for profile')
            else:
                _LOGGER.debug('Could not retrieve consent information')
        except:
            raise
        # Fetch vehicles
        try:
            await self.set_token('vwg')
            self._session_headers.pop('Content-Type', None)
            legacy_vehicles = await self.get(
                url=f'https://msg.volkswagen.de/fs-car/usermanagement/users/v1/{BRAND}/{COUNTRY}/vehicles'
            )

            if legacy_vehicles.get('userVehicles', {}).get('vehicle', False):
                _LOGGER.debug('Found vehicle(s) associated with account.')
                for vehicle in legacy_vehicles.get('userVehicles').get('vehicle'):
                    await self.set_token('vwg')
                    self._session_headers['Accept'] = 'application/vnd.vwg.mbb.vehicleDataDetail_v2_1_0+xml, application/vnd.vwg.mbb.genericError_v1_0_2+xml'
                    response = await self.get(
                        urljoin(
                            self._session_auth_ref_url,
                            f'fs-car/vehicleMgmt/vehicledata/v2/{BRAND}/{COUNTRY}/vehicles/{vehicle}'
                        )
                    )
                    self._session_headers['Accept'] = 'application/json'

                    _LOGGER.debug(f'Response is {response} of type {type(response)}')
                    if response.get('vehicleDataDetail', False):
                        data = {
                            'vin': vehicle,
                            'specification': {
                                'modelCode':         response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:modelCode', ''),
                                'title':             response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:modelName', ''),
                                'manufacturingDate': response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:modelYear', ''),
                                'color':             response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:color', ''),
                                'countryCode':       response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:countryCode', ''),
                                'engine':            response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:engine', ''),
                                'mmi':               response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:mmi', ''),
                                'transmission':      response.get('vehicleDataDetail', {}).get('ns4:carportData', {}).get('ns4:transmission', ''),
                            }
                        }
                        response['vin'] = vehicle
                        api_vehicles.append(data)
                    else:
                        _LOGGER.warning(f"Failed to aquire information about vehicle with VIN {vehicle}")
        except:
            raise

        # If neither API returns any vehicles, raise an error
        if len(api_vehicles) == 0:
            raise SeatConfigException("No vehicles were found for given account!")
        # Get vehicle connectivity information
        else:
            try:
                for vehicle in api_vehicles:
                    _LOGGER.debug(f'Checking vehicle {vehicle}')
                    vin = vehicle.get('vin', '')
                    specs = vehicle.get('specification', vehicle.get('vehicleSpecification', ''))
                    connectivity = []
                    for service in vehicle.get('connectivities', []):
                        if isinstance(service, str):
                            connectivity.append(service)
                        elif isinstance(service, dict):
                            connectivity.append(service.get('type', ''))

                    capabilities = []
                    for capability in vehicle.get('capabilities', []):
                        capabilities.append(capability.get('id', ''))
                    vehicle = {
                        'vin': vin,
                        'connectivities': connectivity,
                        'capabilities': capabilities,
                        'specification': specs,
                    }
                    # Check if object already exist
                    _LOGGER.debug(f'Check if vehicle exists')
                    if self.vehicle(vin) is not None:
                        _LOGGER.debug(f'Vehicle with VIN number {vin} already exist.')
                        car = Vehicle(self, vehicle)
                        if not car == self.vehicle(vehicle):
                            _LOGGER.debug(f'Updating {vehicle} object')
                            self._vehicles.pop(vehicle)
                            self._vehicles.append(Vehicle(self, vehicle))
                    else:
                        _LOGGER.debug(f'Adding vehicle {vin}, with connectivities: {connectivity}')
                        self._vehicles.append(Vehicle(self, vehicle))
            except:
                raise SeatLoginFailedException("Unable to fetch associated vehicles for account")
        # Update data for all vehicles
        await self.update_all()

        return api_vehicles

 #### API get data functions ####
   # Profile related functions
    async def getConsentInfo(self):
        """Get consent information for user."""
        try:
            await self.set_token(BRAND)
            atoken = self._session_tokens[BRAND]['access_token']
            # Try old pyJWT syntax first
            try:
                subject = jwt.decode(atoken, verify=False).get('sub', None)
            except:
                subject = None
            # Try new pyJWT syntax if old fails
            if subject is None:
                try:
                    exp = jwt.decode(atoken, options={'verify_signature': False}).get('sub', None)
                except:
                    raise Exception("Could not extract sub attribute from token")

            data = {'scopeId': 'commonMandatoryFields'}
            response = await self.post(f'https://profileintegrityservice.apps.emea.vwapps.io/iaa/pic/v1/users/{subject}/check-profile', json=data)
            if response.get('mandatoryConsentInfo', False):
                data = {
                    'consentInfo': response
                }
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch realCarData, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unhandled error while trying to fetch consent information')
        except Exception as error:
            _LOGGER.debug(f'Could not get consent information, error {error}')
        return False

    async def getRealCarData(self):
        """Get car information from customer profile, VIN, nickname, etc."""
        try:
            await self.set_token(BRAND)
            _LOGGER.debug("Attempting extraction of jwt subject from identity token.")
            atoken = self._session_tokens[BRAND]['access_token']
            # Try old pyJWT syntax first
            try:
                subject = jwt.decode(atoken, verify=False).get('sub', None)
            except:
                subject = None
            # Try new pyJWT syntax if old fails
            if subject is None:
                try:
                    subject = jwt.decode(atoken, options={'verify_signature': False}).get('sub', None)
                except:
                    raise Exception("Could not extract sub attribute from token")

            response = await self.get(
                f'https://customer-profile.apps.emea.vwapps.io/v2/customers/{subject}/realCarData'
            )
            if isinstance(response, dict):
                if response.get('realCars', False):
                    data = {
                        'realCars': response.get('realCars', {})
                    }
                    return data
                elif response.get('status_code', {}):
                    _LOGGER.warning(f'Could not fetch realCarData, HTTP status code: {response.get("status_code")}')
                else:
                    _LOGGER.debug('Empty response for realCar data')
            else:
                _LOGGER.info('Unhandled error while trying to fetch realcar data')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch realCarData, error: {error}')
        return False

   # Vehicle related functions
    async def getHomeRegion(self, vin):
        """Get API requests base url for VIN."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'https://mal-1a.prd.ece.vwg-connect.com/api/cs/vds/v1/vehicles/{vin}/homeRegion', vin)
            return response.get('homeRegion', {}).get('baseUri', {}).get('content', False)
            #self._session_auth_ref_url = response['homeRegion']['baseUri']['content'].split('/api')[0].replace('mal-', 'fal-') if response['homeRegion']['baseUri']['content'] != 'https://mal-1a.prd.ece.vwg-connect.com/api' else 'https://msg.volkswagen.de'
            #self._session_oper_ref_url = response['homeRegion']['baseUri']['content'].split('/api')[0].replace('mal-', 'fal-') if response['homeRegion']['baseUri']['content'] != 'https://mal-1a.prd.ece.vwg-connect.com/api' else 'https://mal-1a.prd.ece.vwg-connect.com'
            #self._session_spin_ref_url = response['homeRegion']['baseUri']['content'].split('/api')[0]
            #return response['homeRegion']['baseUri']['content']
        except Exception as error:
            _LOGGER.debug(f'Could not get homeregion, error {error}')
        return False

    async def getOperationList(self, vin, baseurl):
        """Collect operationlist for VIN, supported/licensed functions."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/api/rolesrights/operationlist/v3/vehicles/{vin}')
            if response.get('operationList', False):
                data = response.get('operationList', {})
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch operation list, HTTP status code: {response.get("status_code")}')
                data = response
            else:
                _LOGGER.info(f'Could not fetch operation list: {response}')
                data = {'error': 'unknown'}
        except Exception as error:
            _LOGGER.warning(f'Could not fetch operation list, error: {error}')
            data = {'error': 'unknown'}
        return data

    async def getModelImageURL(self, vin):
        """Construct the URL for the model image."""
        try:
            # Construct message to be encrypted
            date = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%mZ')
            message = MODELAPPID +'\n'+ MODELAPI +'?vin='+ vin +'&view='+ MODELVIEW +'&date='+ date
            # Construct hmac SHA-256 key object and encode the message
            digest = hmac.new(MODELAPIKEY, msg=message.encode(), digestmod=hashlib.sha256).digest()
            b64enc = {'sign': b64encode(digest).decode()}
            sign = urlencode(b64enc)
            # Construct the URL
            path = MODELAPI +'?vin='+ vin +'&view='+ MODELVIEW +'&appId='+ MODELAPPID +'&date='+ date +'&'+ sign
            url = MODELHOST + path
            try:
                response = await self._session.get(
                    url=url,
                    allow_redirects=False
                )
                if response.headers.get('Location', False):
                    return response.headers.get('Location')
                else:
                    _LOGGER.debug('Could not fetch Model image URL, request returned with status code {response.status_code}')
            except:
                _LOGGER.debug('Could not fetch Model image URL')
        except:
            _LOGGER.debug('Could not fetch Model image URL, message signing failed.')
        return None

    async def getVehicleStatusReport(self, vin, baseurl):
        """Get stored vehicle status report (Connect services)."""
        try:
            await self.set_token('vwg')
            response = await self.get(
                f'{baseurl}/fs-car/bs/vsr/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/status'
            )
            if response.get('StoredVehicleDataResponse', {}).get('vehicleData', {}).get('data', {})[0].get('field', {})[0] :
                data = {
                    'StoredVehicleDataResponse': response.get('StoredVehicleDataResponse', {}),
                    'StoredVehicleDataResponseParsed': dict([(e['id'],e if 'value' in e else '') for f in [s['field'] for s in response['StoredVehicleDataResponse']['vehicleData']['data']] for e in f])
                }
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch vehicle status report, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unhandled error while trying to fetch status data')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch StoredVehicleDataResponse, error: {error}')
        return False

    async def getTripStatistics(self, vin, baseurl):
        """Get short term trip statistics."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/fs-car/bs/tripstatistics/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/tripdata/shortTerm?newest')
            if response.get('tripData', {}):
                data = {'tripstatistics': response.get('tripData', {})}
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch trip statistics, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info(f'Unhandled error while trying to fetch trip statistics')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch trip statistics, error: {error}')
        return False

    async def getPosition(self, vin, baseurl):
        """Get position data."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/fs-car/bs/cf/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/position')
            if response.get('findCarResponse', {}):
                data = {
                    'findCarResponse': response.get('findCarResponse', {}),
                    'isMoving': False
                }
                return data
            elif response.get('status_code', {}):
                if response.get('status_code', 0) == 204:
                    _LOGGER.debug(f'Seems car is moving, HTTP 204 received from position')
                    data = {
                        'isMoving': True,
                        'rate_limit_remaining': 15
                    }
                    return data
                else:
                    _LOGGER.warning(f'Could not fetch position, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unhandled error while trying to fetch positional data')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch position, error: {error}')
        return False

    async def getDeparturetimer(self, vin, baseurl):
        """Get departure timers."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/fs-car/bs/departuretimer/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/timer')
            if response.get('timer', {}):
                data = {'departuretimer': response.get('timer', {})}
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch timers, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unknown error while trying to fetch data for departure timers')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch timers, error: {error}')
        return False

    async def getClimater(self, vin, baseurl):
        """Get climatisation data."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/fs-car/bs/climatisation/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/climater')
            if response.get('climater', {}):
                data = {'climater': response.get('climater', {})}
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch climatisation, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unhandled error while trying to fetch climatisation data')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch climatisation, error: {error}')
        return False

    async def getCharger(self, vin, baseurl):
        """Get charger data."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/fs-car/bs/batterycharge/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/charger')
            if response.get('charger', {}):
                data = {'charger': response.get('charger', {})}
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch charger, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unhandled error while trying to fetch charger data')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch charger, error: {error}')
        return False

    async def getPreHeater(self, vin, baseurl):
        """Get parking heater data."""
        try:
            await self.set_token('vwg')
            response = await self.get(f'{baseurl}/fs-car/bs/rs/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/status')
            if response.get('statusResponse', {}):
                data = {'heating': response.get('statusResponse', {})}
                return data
            elif response.get('status_code', {}):
                _LOGGER.warning(f'Could not fetch pre-heating, HTTP status code: {response.get("status_code")}')
            else:
                _LOGGER.info('Unhandled error while trying to fetch pre-heating data')
        except Exception as error:
            _LOGGER.warning(f'Could not fetch pre-heating, error: {error}')
        return False

 #### API data set functions ####
    async def get_request_status(self, vin, sectionId, requestId, baseurl):
        """Return status of a request ID for a given section ID."""
        try:
            error_code = None
            # Requests for VW-Group API
            if sectionId == 'climatisation':
                url = f'{baseurl}/fs-car/bs/$sectionId/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/climater/actions/$requestId'
            elif sectionId == 'batterycharge':
                url = f'{baseurl}/fs-car/bs/$sectionId/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/charger/actions/$requestId'
            elif sectionId == 'departuretimer':
                url = f'{baseurl}/fs-car/bs/$sectionId/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/timer/actions/$requestId'
            elif sectionId == 'vsr':
                url = f'{baseurl}/fs-car/bs/$sectionId/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/requests/$requestId/jobstatus'
            elif sectionId == 'rhf':
                url = f'{baseurl}/fs-car/bs/$sectionId/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/honkAndFlash/$requestId/status'
            else:
                url = f'{baseurl}/fs-car/bs/$sectionId/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/requests/$requestId/status'
            url = re.sub('\$sectionId', sectionId, url)
            url = re.sub('\$requestId', requestId, url)

            # Set token
            await self.set_token('vwg')

            response = await self.get(url)
            # Pre-heater on older cars
            if response.get('requestStatusResponse', {}).get('status', False):
                result = response.get('requestStatusResponse', {}).get('status', False)
            # Electric charging, climatisation and departure timers
            elif response.get('action', {}).get('actionState', False):
                result = response.get('action', {}).get('actionState', False)
                error_code = response.get('action', {}).get('errorCode', None)
            else:
                result = 'Unknown'
            # Translate status messages to meaningful info
            if result in ['request_in_progress', 'queued', 'fetched', 'InProgress', 'Waiting']:
                status = 'In progress'
            elif result in ['request_fail', 'failed']:
                status = 'Failed'
                if error_code is not None:
                    # Identified error code for charging, 11 = not connected
                    if sectionId == 'charging' and error_code == 11:
                        _LOGGER.info(f'Request failed, charger is not connected')
                    else:
                        _LOGGER.info(f'Request failed with error code: {error_code}')
            elif result in ['unfetched', 'delayed', 'PollingTimeout']:
                status = 'No response'
            elif result in [ "FailPlugDisconnected", "FailTimerChargingActive" ]:
                status = "Unavailable"
            elif result in ['request_successful', 'succeeded', "Successful"]:
                status = 'Success'
            else:
                status = result
            return status
        except Exception as error:
            _LOGGER.warning(f'Failure during get request status: {error}')
            raise SeatException(f'Failure during get request status: {error}')

    async def get_sec_token(self, vin, spin, action, baseurl):
        """Get a security token, required for certain set functions."""
        secbase = 'https://msg.volkswagen.de'
        if 'fal-3a' in baseurl:
            secbase = baseurl.replace('fal-', 'mal-')
        urls = {
            'lock':    f'{secbase}/api/rolesrights/authorization/v2/vehicles/{vin}/services/rlu_v1/operations/LOCK/security-pin-auth-requested',
            'unlock':  f'{secbase}/api/rolesrights/authorization/v2/vehicles/{vin}/services/rlu_v1/operations/UNLOCK/security-pin-auth-requested',
            'heating': f'{secbase}/api/rolesrights/authorization/v2/vehicles/{vin}/services/rheating_v1/operations/P_QSACT/security-pin-auth-requested',
            'timer':   f'{secbase}/api/rolesrights/authorization/v2/vehicles/{vin}/services/timerprogramming_v1/operations/P_SETTINGS_AU/security-pin-auth-requested',
            'rclima':  f'{secbase}/api/rolesrights/authorization/v2/vehicles/{vin}/services/rclima_v1/operations/P_START_CLIMA_AU/security-pin-auth-requested'
        }
        if not spin:
            raise SeatConfigException('SPIN is required')
        try:
            await self.set_token('vwg')
            if not urls.get(action, False):
                raise SeatException(f'Security token for "{action}" is not implemented')
            response = await self.get(
                urljoin(
                    self._session_spin_ref_url,
                    urls.get(action)
                )
            )
            secToken = response['securityPinAuthInfo']['securityToken']
            challenge = response['securityPinAuthInfo']['securityPinTransmission']['challenge']
            spinHash = self.hash_spin(challenge, str(spin))
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
            response = await self.post(urljoin(self._session_spin_ref_url, '/api/rolesrights/authorization/v2/security-pin-auth-completed'), json = body)
            self._session_headers.pop('Content-Type', None)
            if response.get('securityToken', False):
                return response['securityToken']
            else:
                raise SeatException('Did not receive a valid security token')
        except Exception as error:
            _LOGGER.error(f'Could not generate security token (maybe wrong SPIN?), error: {error}')
            raise

   # VW-Group API methods
    async def _setVWAPI(self, endpoint, **data):
        """Data call through VW-Group API."""
        try:
            await self.set_token('vwg')
            # Combine homeregion with endpoint URL
            url = endpoint #urljoin(self._session_auth_ref_url, endpoint)
            response = await self._data_call(url, **data)
            self._session_headers.pop('X-mbbSecToken', None)
            self._session_headers.pop('X-securityToken', None)
            if not response:
                raise SeatException(f'Invalid or no response for endpoint {endpoint}')
            elif response == 429:
                raise SeatThrottledException('Action rate limit reached. Start the car to reset the action limit')
            else:
                data = {'id': '', 'state': ''}
                for key in response:
                    if isinstance(response.get(key), dict):
                        for k in response.get(key):
                            if 'id' in k.lower():
                                data['id'] = str(response.get(key).get(k))
                            if 'state' in k.lower():
                                data['state'] = response.get(key).get(k)
                    else:
                        if 'Id' in key:
                            data['id'] = str(response.get(key))
                        if 'State' in key:
                            data['state'] = response.get(key)
                if response.get('rate_limit_remaining', False):
                    data['rate_limit_remaining'] = response.get('rate_limit_remaining', None)
                return data
        except:
            self._session_headers.pop('X-mbbSecToken', None)
            self._session_headers.pop('X-securityToken', None)
            raise
        return False

    async def setCharger(self, vin, baseurl, data):
        """Start/Stop charger."""
        return await self._setVWAPI(f'{baseurl}/fs-car/bs/batterycharge/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/charger/actions', json = data)

    async def setClimater(self, vin, baseurl, data, spin):
        """Execute climatisation actions."""
        try:
            # Only get security token if auxiliary heater is to be started
            if data.get('action', {}).get('settings', {}).get('heaterSource', None) == 'auxiliary':
                self._session_headers['X-securityToken'] = await self.get_sec_token(vin=vin, spin=spin, action='rclima', baseurl=baseurl)
            return await self._setVWAPI(f'{baseurl}/fs-car/bs/climatisation/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/climater/actions', json = data)
        except:
            raise
        return False

    async def setDeparturetimer(self, vin, baseurl, data, spin):
        """Set departure timers."""
        try:
            # First get most recent departuretimer settings from server
            departuretimers = await self.getDeparturetimer(vin)
            timer = departuretimers.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerList', {}).get('timer', [])
            profile = departuretimers.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerProfileList', {}).get('timerProfile', [])
            setting = departuretimers.get('departuretimer', {}).get('timersAndProfiles', {}).get('timerBasicSetting', [])

            # Construct Timer data
            timers = [{},{},{}]
            for i in range(0, 3):
                timers[i]['currentCalendarProvider'] = {}
                for key in timer[i]:
                    # Ignore the timestamp key
                    if key not in ['timestamp']:
                        timers[i][key] = timer[i][key]
                if timers[i].get('timerFrequency', '') == 'single':
                    timers[i]['departureTimeOfDay'] = '00:00'

            # Set charger minimum limit if action is chargelimit
            if data.get('action', None) == 'chargelimit' :
                actiontype = 'setChargeMinLimit'
                setting['chargeMinLimit'] = int(data.get('limit', 50))
            # Modify timers if action is on, off or schedule
            elif data.get('action', None) in ['on', 'off', 'schedule']:
                actiontype = 'setTimersAndProfiles'
                if 'id' in data:
                    timerid = int(data.get('id', 1)) -1
                else:
                    timerid = int(data.get('schedule', {}).get('id', 1))-1

                # Set timer programmed status if data contains action = on or off
                if data.get('action', None) in ['on', 'off']:
                    action = 'programmed' if data.get('action', False) == 'on' else 'notProgrammed'
                # Set departure schedule
                elif data.get('action', None) == 'schedule':
                    action = 'programmed' if data.get('schedule', {}).get('enabled', False) else 'notProgrammed'
                    if data.get('schedule', {}).get('recurring', False):
                        timers[timerid]['timerFrequency'] = 'cyclic'
                        timers[timerid]['departureWeekdayMask'] = data.get('schedule', {}).get('days', 'nnnnnnn')
                        timers[timerid]['departureTimeOfDay'] = data.get('schedule', {}).get('time', '08:00')
                        timers[timerid].pop('departureDateTime', None)
                    else:
                        timers[timerid]['timerFrequency'] = 'single'
                        timers[timerid]['departureWeekdayMask'] = 'nnnnnnn'
                        timers[timerid]['departureTimeOfDay'] = '00:00'
                        timers[timerid]['departureDateTime'] = \
                            data.get('schedule', {}).get('date', '2020-01-01') + 'T' +\
                            data.get('schedule', {}).get('time', '08:00')
                # Catch uncatched scenario
                else:
                    action = 'notProgrammed'
                timers[timerid]['timerProgrammedStatus'] = action
            else:
                raise SeatException('Unknown action for departure timer')

            # Construct Profiles data
            profiles = [{},{},{}]
            for i in range(0, 3):
                for key in profile[i]:
                    # Ignore the timestamp key
                    if key not in ['timestamp']:
                        profiles[i][key] = profile[i][key]

            # Set optional settings
            if data.get('schedule', {}).get('chargeMaxCurrent', None) is not None:
                profiles[timerid]['chargeMaxCurrent']=data.get('schedule', {}).get('chargeMaxCurrent',False)

            if data.get('schedule', {}).get('targetChargeLevel', None) is not None:
                profiles[timerid]['targetChargeLevel']=data.get('schedule', {}).get('targetChargeLevel',False)

            if data.get('schedule', {}).get('profileName', None) is not None:
                profiles[timerid]['profileName']=data.get('schedule', {}).get('profileName',False)

            if data.get('schedule', {}).get('operationClimatisation', None) is not None:
                profiles[timerid]['operationClimatisation']=data.get('schedule', {}).get('operationClimatisation',False)

            if data.get('schedule', {}).get('operationCharging', None) is not None:
                profiles[timerid]['operationCharging']=data.get('schedule', {}).get('operationCharging',False)

            # Construct basic settings
            settings = {
                'chargeMinLimit': int(setting['chargeMinLimit']),
                'heaterSource': 'electric',
                'targetTemperature': int(data['temp'])
            }
            body = {
                'action': {
                    'timersAndProfiles': {
                        'timerBasicSetting': settings,
                        'timerList': {
                            'timer': timers
                        },
                        'timerProfileList': {
                            'timerProfile': profiles
                        }
                    },
                    'type': actiontype
                }
            }
            await self.set_token('vwg')
            # Only get security token if auxiliary heater is to be enabled
            #if data.get... == 'auxiliary':
            #   self._session_headers['X-securityToken'] = await self.get_sec_token(vin = vin, spin = spin, action = 'timer')
            return await self._setVWAPI(f'{baseurl}/fs-car/bs/departuretimer/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/timer/actions', json = body)
        except:
            raise
        return False

    async def setHonkAndFlash(self, vin, baseurl, data):
        """Execute honk and flash actions."""
        return await self._setVWAPI(f'{baseurl}/fs-car/bs/rhf/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/honkAndFlash', json = data)

    async def setLock(self, vin, baseurl, data, spin):
        """Remote lock and unlock actions."""
        try:
            # Save Content-Type header to be restored later
            if 'Content-Type' in self._session_headers:
                contType = self._session_headers['Content-Type']
            else:
                contType = False
            # Fetch security token for lock/unlock
            if 'unlock' in data:
                self._session_headers['X-mbbSecToken'] = await self.get_sec_token(vin=vin, spin=spin, action='unlock', baseurl=baseurl)
            else:
                self._session_headers['X-mbbSecToken'] = await self.get_sec_token(vin=vin, spin=spin, action='lock', baseurl=baseurl)
            # Set temporary Content-Type
            self._session_headers['Content-Type'] = 'application/vnd.vwg.mbb.RemoteLockUnlock_v1_0_0+xml'

            response = await self._setVWAPI(f'{baseurl}/fs-car/bs/rlu/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/actions', data = data)

            # Clean up content-type
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType

            return response

        except:
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType
            raise
        return False

    async def setPreHeater(self, vin, baseurl, data, spin):
        """Petrol/diesel parking heater actions."""
        try:
            if 'Content-Type' in self._session_headers:
                contType = self._session_headers['Content-Type']
            else:
                contType = ''
            self._session_headers['Content-Type'] = 'application/vnd.vwg.mbb.RemoteStandheizung_v2_0_2+json'
            if isinstance(data, dict):
                if not 'quickstop' in data.get('performAction'):
                    self._session_headers['x-mbbSecToken'] = await self.get_sec_token(vin=vin, spin=spin, action='heating', baseurl=baseurl)
            else:
                raise SeatConfigException("Invalid data for preheater")
            response = await self._setVWAPI(f'{baseurl}/fs-car/bs/rs/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/action', json = data)

            # Clean up headers
            self._session_headers.pop('x-mbbSecToken', None)
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType

            return response

        except Exception as error:
            self._session_headers.pop('x-mbbSecToken', None)
            self._session_headers.pop('Content-Type', None)
            if contType: self._session_headers['Content-Type'] = contType
            raise
        return False

    async def setRefresh(self, vin, baseurl):
        """"Force vehicle data update."""
        return await self._setVWAPI(f'{baseurl}/fs-car/bs/vsr/v1/{BRAND}/{COUNTRY}/vehicles/{vin}/requests', data=None)

 #### Token handling ####
    async def validate_token(self, token):
        """Function to validate a single token."""
        try:
            now = datetime.now()
            # Try old pyJWT syntax first
            try:
                exp = jwt.decode(token, verify=False).get('exp', None)
            except:
                exp = None
            # Try new pyJWT syntax if old fails
            if exp is None:
                try:
                    exp = jwt.decode(token, options={'verify_signature': False}).get('exp', None)
                except:
                    raise Exception("Could not extract exp attribute")

            expires = datetime.fromtimestamp(int(exp))

            # Lazy check but it's very inprobable that the token expires the very second we want to use it
            if expires > now:
                return expires
            else:
                _LOGGER.debug(f'Token expired at {expires.strftime("%Y-%m-%d %H:%M:%S")})')
                return False
        except Exception as e:
            _LOGGER.info(f'Token validation failed, {e}')
        return False

    async def verify_token(self, token):
        """Function to verify a single token."""
        try:
            req = None
            # Try old pyJWT syntax first
            try:
                aud = jwt.decode(token, verify=False).get('aud', None)
            except:
                aud = None
            # Try new pyJWT syntax if old fails
            if aud is None:
                try:
                    aud = jwt.decode(token, options={'verify_signature': False}).get('aud', None)
                except:
                    raise Exception("Could not extract exp attribute")

            if not isinstance(aud, str):
                aud = next(iter(aud))
            _LOGGER.debug(f"Verifying token for {aud}")
            # If audience indicates a client from https://identity.vwgroup.io
            for client in CLIENT_LIST:
                if self._session_fulldebug:
                    _LOGGER.debug(f"Matching {aud} against {CLIENT_LIST[client].get('CLIENT_ID', '')}")
                if aud == CLIENT_LIST[client].get('CLIENT_ID', ''):
                    req = await self._session.get(url = 'https://identity.vwgroup.io/oidc/v1/keys')
                    break

            # If no match for "BRAND" clients, assume token is issued from https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth
            if req is None:
                req = await self._session.get(url = 'https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/public/jwk/v1')

            # Fetch key list
            keys = await req.json()
            pubkeys = {}
            # Convert all RSA keys and store in list
            for jwk in keys['keys']:
                kid = jwk['kid']
                if jwk['kty'] == 'RSA':
                    pubkeys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(to_json(jwk))
            # Get key ID from token and get match from key list
            token_kid = jwt.get_unverified_header(token)['kid']
            if self._session_fulldebug:
                try:
                    _LOGGER.debug(f'Token Key ID is {token_kid}, match from public keys: {keys["keys"][token_kid]}')
                except:
                    pass
            pubkey = pubkeys[token_kid]

            # Verify token with public key
            if jwt.decode(token, key=pubkey, algorithms=['RS256'], audience=aud):
                return True
        except ExpiredSignatureError:
            return False
        except Exception as error:
            _LOGGER.debug(f'Failed to verify {aud} token, error: {error}')
            return error

    async def refresh_token(self, client):
        """Function to refresh tokens for a client."""
        try:
            # Refresh API tokens
            _LOGGER.debug(f'Refreshing tokens for client "{client}"')
            if client != 'vwg':
                body = {
                    'grant_type': 'refresh_token',
                    'brand': BRAND,
                    'refresh_token': self._session_tokens[client]['refresh_token']
                }
                url = 'https://tokenrefreshservice.apps.emea.vwapps.io/refreshTokens'
            else:
                body = {
                    'grant_type': 'refresh_token',
                    'scope': 'sc2:fal',
                    'token': self._session_tokens[client]['refresh_token']
                }
                url = 'https://mbboauth-1d.prd.ece.vwg-connect.com/mbbcoauth/mobile/oauth2/v1/token'

            try:
                response = await self._session.post(
                    url=url,
                    headers=TOKEN_HEADERS.get(client),
                    data = body,
                )
            except:
                raise

            if response.status == 200:
                tokens = await response.json()
                # Verify access_token
                if 'access_token' in tokens:
                    if not await self.verify_token(tokens['access_token']):
                        _LOGGER.warning('Tokens could not be verified!')
                for token in tokens:
                    self._session_tokens[client][token] = tokens[token]
                return True
            elif response.status == 400:
                error = await response.json()
                if error.get('error', {}) == 'invalid_grant':
                    _LOGGER.debug(f'VW-Group API token refresh failed: {error.get("error_description", {})}')
                    if client == 'vwg':
                        return await self._getAPITokens()
            else:
                resp = await response.json()
                _LOGGER.warning(f'Something went wrong when refreshing tokens for "{client}".')
                _LOGGER.debug(f'Headers: {TOKEN_HEADERS.get("vwg")}')
                _LOGGER.debug(f'Request Body: {body}')
                _LOGGER.warning(f'Something went wrong when refreshing VW-Group API tokens.')
        except Exception as error:
            _LOGGER.warning(f'Could not refresh tokens: {error}')
        return False

    async def set_token(self, client):
        """Switch between tokens."""
        # Lock to prevent multiple instances updating tokens simultaneously
        async with self._lock:
            # If no tokens are available for client, try to authorize
            tokens = self._session_tokens.get(client, None)
            if tokens is None:
                _LOGGER.debug(f'Client "{client}" token is missing, call to authorize the client.')
                try:
                    # Try to authorize client and get tokens
                    if client != 'vwg':
                        result = await self._authorize(client)
                    else:
                        result = await self._getAPITokens()

                    # If authorization wasn't successful
                    if result is not True:
                        raise SeatAuthenticationException(f'Failed to authorize client {client}')
                except:
                    raise
            try:
                # Validate access token for client, refresh if validation fails
                valid = await self.validate_token(self._session_tokens.get(client, {}).get('access_token', ''))
                if not valid:
                    _LOGGER.debug(f'Tokens for "{client}" are invalid')
                    # Try to refresh tokens for client
                    if await self.refresh_token(client) is not True:
                        raise SeatTokenExpiredException(f'Tokens for client {client} are invalid')
                    else:
                        _LOGGER.debug(f'Tokens refreshed successfully for client "{client}"')
                        pass
                else:
                    try:
                        dt = datetime.fromtimestamp(valid)
                        _LOGGER.debug(f'Access token for "{client}" is valid until {dt.strftime("%Y-%m-%d %H:%M:%S")}')
                    except:
                        pass
                # Assign token to authorization header
                self._session_headers['Authorization'] = 'Bearer ' + self._session_tokens[client]['access_token']
                if client == 'seat':
                    self._session_headers['tokentype'] = 'IDK_TECHNICAL'
                elif client == 'skoda':
                    self._session_headers['tokentype'] = 'IDK_TECHNICAL'
                elif client == 'connect':
                    self._session_headers['tokentype'] = 'IDK_CONNECT'
                elif client == 'smartlink':
                    self._session_headers['tokentype'] = 'IDK_SMARTLINK'
                else:
                    self._session_headers['tokentype'] = 'MBB'
            except:
                raise SeatException(f'Failed to set token for "{client}"')
            return True

 #### Class helpers ####
    @property
    def vehicles(self):
        """Return list of Vehicle objects."""
        return self._vehicles

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
        if await connection.doLogin():
            if await connection.get_vehicles():
                for vehicle in connection.vehicles:
                    print(f'Vehicle id: {vehicle}')
                    print('Supported sensors:')
                    for instrument in vehicle.dashboard().instruments:
                        print(f' - {instrument.name} (domain:{instrument.component}) - {instrument.str_state}')


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
