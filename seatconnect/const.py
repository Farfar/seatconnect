"""Constants for Seat Connect library."""

BASE_SESSION = 'https://msg.volkswagen.de'
BASE_AUTH = 'https://identity.vwgroup.io'
CLIENT_ID = '50f215ac-4444-4230-9fb1-fe15cd1a9bcc@apps_vw-dilab_com'
XCLIENT_ID = '3516bc10-fabd-4eb2-b41c-b38e21e9d8f6'
XAPPVERSION = '1.2.0'
XAPPNAME = 'SEATConnect'
USER_AGENT = 'okhttp/3.10.0'
APP_URI = 'seatconnect://identity-kit/login'

HEADERS_SESSION = {
    'Connection': 'keep-alive',
    'Content-Type': 'application/json',
    'Accept-charset': 'UTF-8',
    'Accept': 'application/json',
    'X-Client-Id': XCLIENT_ID,
    'X-App-Version': XAPPVERSION,
    'X-App-Name': XAPPNAME,
    'User-Agent': USER_AGENT
}

HEADERS_AUTH = {
    'Connection': 'keep-alive',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
#    'application/json,text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,\
#        image/apng,*/*;q=0.8,application/signed-exchange;v=b3',
    'Content-Type': 'application/x-www-form-urlencoded',
    'x-requested-with' = 'com.seat.connectedcar.mod2connectapp',
    'User-Agent': USER_AGENT,
    'X-App-Name': XAPPNAME
}
