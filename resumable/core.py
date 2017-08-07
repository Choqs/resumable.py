import os
from enum import Enum
import uuid
import mimetypes

import requests

from resumable.file import LazyLoadChunkableFile
from resumable.worker import ResumableWorkerPool
from resumable.util import CallbackMixin, FixedUrlSession


MB = 1024 * 1024


class ResumableSignal(Enum):
    CHUNK_COMPLETED = 0
    FILE_ADDED = 1
    FILE_COMPLETED = 2


class Resumable(CallbackMixin):

    def __init__(self, target, simultaneous_uploads=3, chunk_size=MB,
                 headers=None):
        super(Resumable, self).__init__()
        self.target = target
        self.chunk_size = chunk_size

        self.session = requests.Session()

        # TODO: Set User-Agent as python-resumable/version
        if headers:
            self.session.headers.update(headers)

        self.files = []

        self.worker_pool = ResumableWorkerPool(simultaneous_uploads,
                                               self.next_task)

    def add_file(self, path):
        lazy_load_file = LazyLoadChunkableFile(path, self.chunk_size)
        file = ResumableFile(lazy_load_file)
        self.files.append(file)
        self.send_signal(ResumableSignal.FILE_ADDED)
        file.proxy_signals_to(self)

    def wait_until_complete(self):
        self.worker_pool.join()

    def close(self):
        for file in self.files:
            file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.wait_until_complete()
        self.close()

    @property
    def chunks(self):
        for file in self.files:
            for chunk in file.chunks:
                yield chunk

    def next_task(self):
        for chunk in self.chunks:
            if chunk.state == ResumableChunkState.QUEUED:
                wrapped_session = FixedUrlSession(self.session, self.target)
                return chunk.create_task(wrapped_session)


class ResumableFile(CallbackMixin):

    def __init__(self, file):
        super(ResumableFile, self).__init__()

        self.file = file
        self.unique_identifier = uuid.uuid4()

        self.chunks = [ResumableChunk(self, chunk)
                       for chunk in self.file.chunks]

        for chunk in self.chunks:
            chunk.proxy_signals_to(self)
            chunk.register_callback(ResumableSignal.CHUNK_COMPLETED,
                                    self.handle_chunk_completion)

    def close(self):
        self.file.close()

    @property
    def type(self):
        """Mimic the type parameter of a JS File object.

        Resumable.js uses the File object's type attribute to guess mime type,
        which is guessed from file extention accoring to
        https://developer.mozilla.org/en-US/docs/Web/API/File/type.
        """
        type_, _ = mimetypes.guess_type(self.file.path)
        # When no type can be inferred, File.type returns an empty string
        return '' if type_ is None else type_

    @property
    def query(self):
        return {
            'resumableChunkSize': self.file.chunk_size,
            'resumableTotalSize': self.file.size,
            'resumableType': self.type,
            'resumableIdentifier': str(self.unique_identifier),
            'resumableFileName': os.path.basename(self.file.path),
            'resumableRelativePath': self.file.path,
            'resumableTotalChunks': len(self.chunks)
        }

    @property
    def completed(self):
        for chunk in self.chunks:
            if chunk.state != ResumableChunkState.DONE:
                return False
        else:
            return True

    def handle_chunk_completion(self):
        if self.completed:
            self.send_signal(ResumableSignal.FILE_COMPLETED)
            self.close()


class ResumableChunkState(Enum):
    QUEUED = 0
    POPPED = 1
    UPLOADING = 2
    DONE = 3


class ResumableChunk(CallbackMixin):

    def __init__(self, file, chunk):
        super(ResumableChunk, self).__init__()
        self.file = file
        self.chunk = chunk
        self.state = ResumableChunkState.QUEUED

    def __eq__(self, other):
        return (isinstance(other, ResumableChunk) and
                self.file == other.file and
                self.chunk == other.chunk and
                self.state == other.state)

    @property
    def query(self):
        query = {
            'resumableChunkNumber': self.chunk.index + 1,
            'resumableCurrentChunkSize': self.chunk.size
        }
        query.update(self.file.query)
        return query

    def test(self, session):
        response = session.get(data=self.query)
        if response.status_code == 200:
            self.state = ResumableChunkState.DONE
            self.send_signal(ResumableSignal.CHUNK_COMPLETED)

    def send(self, session):
        self.state = ResumableChunkState.UPLOADING
        response = session.post(data=self.query,
                                files={'file': self.chunk.data})
        response.raise_for_status()
        self.state = ResumableChunkState.DONE
        self.send_signal(ResumableSignal.CHUNK_COMPLETED)

    def send_if_not_done(self, session):
        if self.state != ResumableChunkState.DONE:
            self.send(session)

    def create_task(self, session):
        def task():
            self.test(session)
            self.send_if_not_done(session)
        self.state = ResumableChunkState.POPPED
        return task
