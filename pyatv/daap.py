"""Methods used to GET/POST data from/to an Apple TV."""

import binascii
import logging
import asyncio

from copy import copy
from aiohttp import ClientSession

from pyatv import (dmap, exceptions)
from pyatv.tag_definitions import lookup_tag

_LOGGER = logging.getLogger(__name__)

_DMAP_HEADERS = {
    'Accept': '*/*',
    'Accept-Encoding': 'gzip',
    'Client-DAAP-Version': '3.12',
    'Client-ATV-Sharing-Version': '1.2',
    'Client-iTunes-Sharing-Version': '3.10',
    'User-Agent': 'TVRemote/186 CFNetwork/808.1.4 Darwin/16.1.0',
    'Viewer-Only-Client': '1',
}


DEFAULT_TIMEOUT = 10.0  # Seconds


class DaapSession(object):
    """This class makes it easy to perform DAAP requests.

    It automatically adds the required headers and also does DMAP parsing.
    """

    def __init__(self, loop, timeout=DEFAULT_TIMEOUT):
        """Initialize a new DaapSession."""
        self._session = ClientSession(loop=loop)
        self._timeout = timeout

    def close(self):
        """Close the underlying client session."""
        return self._session.close()

    @asyncio.coroutine
    def get_data(self, url, should_parse=True):
        """Perform a GET request. Optionally parse reponse as DMAP data."""
        _LOGGER.debug('GET URL: %s', url)
        resp = yield from self._session.get(
            url, headers=_DMAP_HEADERS, timeout=self._timeout)
        try:
            resp_data = yield from resp.read()
            extracted = self._extract_data(resp_data, should_parse)
            return extracted, resp.status
        except Exception as ex:
            resp.close()
            raise ex
        finally:
            yield from resp.release()

    @asyncio.coroutine
    def post_data(self, url, data=None, parse=True):
        """Perform a POST request. Optionally parse reponse as DMAP data."""
        _LOGGER.debug('POST URL: %s', url)

        headers = copy(_DMAP_HEADERS)
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

        resp = yield from self._session.post(
            url, headers=headers, data=data, timeout=self._timeout)
        try:
            resp_data = yield from resp.read()
            extracted = self._extract_data(resp_data, parse)
            return extracted, resp.status
        except Exception as ex:
            resp.close()
            raise ex
        finally:
            yield from resp.release()

    @staticmethod
    def _extract_data(data, should_parse):
        if _LOGGER.isEnabledFor(logging.DEBUG):
            output = data[0:128]
            _LOGGER.debug('Data[%d]: %s%s',
                          len(data),
                          binascii.hexlify(output),
                          '...' if len(output) != len(data) else '')
        if should_parse:
            return dmap.parse(data, lookup_tag)
        else:
            return data


class DaapRequester:
    """Helper class that makes it easy to perform DAAP requests.

    It will automatically do login and other necesarry book-keeping.
    """

    def __init__(self, session, address, hsgid, port):
        """Initialize a new DaapRequester."""
        self.session = session
        self.url = 'http://{}:{}'.format(address, port)
        self._hsgid = hsgid
        self._session_id = 0
        self._revision_number = 0  # Not used yet

    @asyncio.coroutine
    def login(self):
        """Login to Apple TV using specified HSGID."""
        # Do not use session.get_data(...) in login as that would end up in
        # an infinte loop.
        url = self._mkurl('login?[AUTH]&hasFP=1', session=False)
        resp = yield from self._do(self.session.get_data, url)
        self._session_id = dmap.first(resp, 'mlog', 'mlid')
        _LOGGER.info('Logged in and got session id %s', self._session_id)
        return self._session_id

    @asyncio.coroutine
    def get(self, cmd, daap_data=True, **args):
        """Perform a DAAP GET command."""
        def _get_request(url):
            return self.session.get_data(url, should_parse=daap_data)

        yield from self._assure_logged_in()
        return (yield from self._do(_get_request,
                                    self._mkurl(cmd, *args),
                                    is_daap=daap_data))

    @asyncio.coroutine
    def post(self, cmd, data=None, **args):
        """Perform DAAP POST command with optional data."""
        def _post_request(url):
            return self.session.post_data(url, data=data)

        yield from self._assure_logged_in()
        return (yield from self._do(_post_request, self._mkurl(cmd, *args)))

    # TODO: refactor to fix this
    # pylint: disable=too-many-arguments
    @asyncio.coroutine
    def _do(self, action, url, retry=True, is_login=False, is_daap=True):
        resp, status = yield from action(url)
        self._log_response(str(action.__name__) + ': %s', resp, is_daap)
        if status >= 200 and status < 300:
            return resp

        # Retry once if we got a bad response, otherwise bail out
        if retry:
            return (yield from self._do(
                action, url, False, is_login=is_login, is_daap=is_daap))
        else:
            raise exceptions.AuthenticationError(
                'failed to login: ' + str(status))

    def _mkurl(self, cmd, *args, session=True, hsgid=True, revision=False):
        url = '{}/{}'.format(self.url, cmd.format(*args))
        auth = ''
        if hsgid:
            auth = 'hsgid=' + self._hsgid
        if session:
            auth = 'session-id={}&{}'.format(self._session_id, auth)
        if revision:
            auth = '{}&revision-number={}'.format(auth, self._revision_number)
        return url.replace('[AUTH]', auth)

    @asyncio.coroutine
    def _assure_logged_in(self):
        if self._session_id != 0:
            _LOGGER.debug('Already logged in, re-using seasion id %d',
                          self._session_id)
        else:
            yield from self.login()

    @staticmethod
    def _log_response(text, data, is_daap):
        if _LOGGER.isEnabledFor(logging.INFO):
            formatted = data
            if is_daap:
                formatted = dmap.pprint(data, lookup_tag)
            _LOGGER.debug(text, formatted)
