# -*- coding: utf-8 -*-

import md5
import re
import urllib2
from StringIO import StringIO

from client import *
from brive import *


class User:

    def __init__(self, login, client):
        self._login = login
        self._client = client
        self._documents = None

    def __repr__(self):
        return self._login

    @property
    def login(self):
        return self._login

    @property
    def documents(self):
        if self._documents is None:
            self._fetch_docs_list()
        return self._documents

    def save_documents(self, backend):
        verbose('Processing docs for {}'.format(self.login))
        # list of processed ids
        done = list()
        # keep track of errors that happen twice in a row
        second_error = False
        self._fetch_docs_list()
        while self._documents:
            document = self._documents.pop()
            verbose('Processing {}\'s doc "{}" (id: {})'.format(
                self.login, document.title, document.id
            ))
            try:
                if not backend.need_to_fetch_contents(self, document):
                    # mark as done, and get to the next one
                    verbose(
                        'Not necessary to fetch doc id '.format(document.id)
                    )
                    done.append(document.id)
                    continue
                contents = document.fetch_contents(self._client)
                second_error = False
            except ExpiredTokenException:
                if second_error:
                    raise Exception(
                        'Two oauth errors in a row while processing'
                        + '{}\'s documents '.format(self.login)
                        + '(doc id: {}), '.format(document.id)
                        + 're-authentication failed'
                    )
                else:
                    second_error = True
                    self._fetch_docs_list(done)
                    continue
            except Exception as ex:
                explanation = \
                    'Unexpected error when processing ' \
                    + '{}\'s documents '.format(self.login) \
                    + '(doc id: {})'.format(document.id)
                ex.brive_explanation = explanation
                raise
            try:
                verbose('Saving {}\'s doc "{}" (id: {})'.format(
                    self.login, document.title, document.id
                ))
                backend.save(self, document)
            except Exception as ex:
                explanation = \
                    'Unexpected error when saving ' \
                    + '{}\'s documents '.format(self.login) \
                    + '(doc id: {})'.format(document.id)
                ex.brive_explanation = explanation
                raise
            # no need to keep the potentially big document's contents in memory
            document.del_contents()
            # mark as done
            done.append(document.id)

    # fetches the documents' list, except those whose ids are in 'done'
    def _fetch_docs_list(self, done=list()):
        debug('Fetching doc list for {}'.format(self.login))
        client = self._client
        client.authorize(self)
        drive_service = client.build_service('drive', 'v2')
        docs_list = drive_service.files().list().execute()
        self._documents = [Document(meta) for meta in docs_list['items']
                           if meta['id'] not in done]


class Document:

    _name_from_header_regex = re.compile('^attachment;\s*filename="([^"]+)"')
    _split_extension_regex = re.compile('(^.*)\.([^.]+)$')

    # if a download fails, we'll re-try it that many times at most
    _max_download_tries = 3

    def __init__(self, meta):
        self._meta = meta
        self._contents = None

    def __repr__(self):
        result = 'Meta: {}'.format(self._meta)
        if self._contents is None:
            result += '\nNo contents\n'
        else:
            result += '\nContents: {}\n'.format(self._contents)
        return result

    @property
    def id(self):
        return self.get_meta('id')

    @property
    def contents(self):
        return self._contents

    @property
    def title(self):
        return self.get_meta('title')

    # sets contents to be a dict mapping file names to contents
    # force_refresh = True forces to re-fetch the contents even if we have
    # already done so
    def fetch_contents(self, client, **kwargs):
        debug('Fetching contents for doc id {}'.format(self.id))
        if self._contents is None \
            or 'force_refresh' in kwargs \
                and kwargs['force_refresh']:
            self._contents = dict()
            for url in self._get_download_urls():
                file_name, content = self._download_from_url(client, url)
                self._contents[file_name] = content

    def del_contents(self):
        self._contents = None

    def get_meta(self, key, default=None):
        if key in self._meta:
            return self._meta[key]
        return default

    def _get_download_urls(self):
        if 'downloadUrl' in self._meta:
            return [self._meta['downloadUrl']]
        elif 'exportLinks' in self._meta:
            return self._meta['exportLinks'].values()
        else:
            verbose('No download URL for document id {}'.format(self.id))
            return []

    def _download_from_url(self, client, url, try_nb=1):
        try:
            headers, content = client.request(url)
            self._check_download_integrity(headers, content)
            return self._get_file_name(headers), content
        except KeyError:
            # token expired
            raise ExpiredTokenException()
        except Exception:
            if try_nb < Document._max_download_tries:
                raise
            return self._download_from_url(client, url, try_nb + 1)

    def _check_download_integrity(self, headers, content):
        debug('Checking download integrity for doc id {}'.format(self.id))
        success, message = True, None
        # content length
        content_length = int(headers.get('content-length', 0))
        if content_length and content_length != StringIO(content).len:
            success = False
            message = 'expected length {} VS actual length {}'.format(
                content_length, len(content)
            )
        # md5 check
        expected_sum = self.get_meta('md5Checksum')
        if success and expected_sum:
            md5_object = md5.new()
            md5_object.update(content)
            actual_sum = md5_object.hexdigest()
            if expected_sum != actual_sum:
                success = False
                message = 'expected md5 sum {} VS actual {}'.format(
                    expected_sum, actual_sum
                )
        if not success:
            raise Exception(
                'Failed to download document id {}: {}'.format(
                    self.id, message
                )
            )

    def _get_file_name(self, headers):
        # get from the headers
        content_disposition = headers['content-disposition']
        results = Document._name_from_header_regex.findall(
            content_disposition
        )
        if not results:
            raise Exception(
                'Unexpected "content_disposition" header: {}'.format(
                    content_disposition
                )
            )
        # urldecode
        result = urllib2.unquote(results[0])
        # insert the doc id in the name (just before the extension)
        # to make sure it's unique
        extension_matches = Document._split_extension_regex.findall(result)
        if extension_matches:
            name, extension = extension_matches[0]
            result = '{}_{}.{}'.format(name, self.id, extension)
        else:
            # no extension (shouldn't happen as far as I can tell)
            result += '_{}'.format(self.id)
        return result
