import asyncio
import threading
import time
import unittest
from types import SimpleNamespace

from live_transcription import LIVE_MODEL, LiveTranscriber


def server_message(
    *, final=None, interim=None, turn_complete=False, generation_complete=False
):
    final_part = SimpleNamespace(text=final) if final is not None else None
    interim_part = SimpleNamespace(text=interim) if interim is not None else None
    return SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=final_part,
            interim_input_transcription=interim_part,
            turn_complete=turn_complete,
            generation_complete=generation_complete,
        )
    )


class FakeSession:
    def __init__(self, messages=(), *, early=False, hang=False):
        self.messages = list(messages)
        self.early = early
        self.hang = hang
        self.sent = []
        self.activity_end = asyncio.Event()
        self.first_audio = threading.Event()

    async def send_realtime_input(self, **kwargs):
        self.sent.append(kwargs)
        if kwargs.get("audio") is not None:
            self.first_audio.set()
        if kwargs.get("activity_end") is not None:
            self.activity_end.set()

    async def receive(self):
        if not self.early:
            await self.activity_end.wait()
        if self.hang:
            await asyncio.Event().wait()
        for message in self.messages:
            yield message


class FakeConnection:
    def __init__(self, session, *, connect_gate=None):
        self.session = session
        self.connect_gate = connect_gate
        self.exited = False

    async def __aenter__(self):
        if self.connect_gate is not None:
            while not self.connect_gate.is_set():
                await asyncio.sleep(0.002)
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class FakeLive:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        return self.connection


class FakeAsyncClient:
    def __init__(self, connection):
        self.live = FakeLive(connection)
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeClient:
    def __init__(self, session, *, connect_gate=None):
        self.connection = FakeConnection(session, connect_gate=connect_gate)
        self.aio = FakeAsyncClient(self.connection)
        self.closed = False

    def close(self):
        self.closed = True


class ClientFactory:
    def __init__(self, client):
        self.client = client
        self.keys = []

    def __call__(self, api_key):
        self.keys.append(api_key)
        return self.client


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class LiveTranscriberTests(unittest.TestCase):
    def make_transcriber(self, session, **kwargs):
        client = FakeClient(session, connect_gate=kwargs.pop("connect_gate", None))
        options = {
            "connect_timeout": 0.5,
            "send_timeout": 0.5,
            "receive_timeout": 0.5,
            "poll_interval": 0.001,
        }
        options.update(kwargs)
        transcriber = LiveTranscriber(
            "test-key",
            client_factory=ClientFactory(client),
            **options,
        )
        return transcriber, client

    def test_streams_audio_before_stop_then_returns_completed_turn(self):
        session = FakeSession(
            [
                server_message(final="salam "),
                server_message(final="dunya", turn_complete=True),
            ]
        )
        live, client = self.make_transcriber(session)

        live.start()
        self.assertTrue(live.feed_audio(b"first"))
        self.assertTrue(session.first_audio.wait(0.5))
        self.assertFalse(any(call.get("activity_end") for call in session.sent))
        self.assertTrue(live.feed_audio(b"second"))

        self.assertEqual(live.finish(1.0), "salam dunya")
        audio = [call["audio"].data for call in session.sent if call.get("audio")]
        self.assertEqual(audio, [b"first", b"second"])
        self.assertEqual(client.aio.live.calls[0]["model"], LIVE_MODEL)
        self.assertFalse(live.failed)
        self.assertTrue(client.aio.closed)
        self.assertTrue(client.closed)
        self.assertTrue(client.connection.exited)

    def test_keeps_repeated_final_segments_and_ignores_interim_text(self):
        session = FakeSession(
            [
                server_message(interim="wrong draft"),
                server_message(final="go "),
                server_message(final="go "),
                server_message(final="now", turn_complete=True),
            ]
        )
        live, _ = self.make_transcriber(session)
        live.start()
        live.feed_audio(b"pcm")

        self.assertEqual(live.finish(1.0), "go go now")

    def test_generation_complete_finishes_dedicated_transcription_turn(self):
        session = FakeSession(
            [
                server_message(final="salam "),
                server_message(final="dunya"),
                server_message(generation_complete=True),
            ]
        )
        live, client = self.make_transcriber(
            session, language_codes=("az-AZ",)
        )
        live.start()
        live.feed_audio(b"pcm")

        self.assertEqual(live.finish(1.0), "salam dunya")
        config = client.aio.live.calls[0]["config"]
        self.assertEqual(
            config.input_audio_transcription.language_codes, ["az-AZ"]
        )

    def test_interim_only_generation_complete_does_not_become_final_text(self):
        session = FakeSession(
            [
                server_message(interim="speculative"),
                server_message(generation_complete=True),
            ]
        )
        live, _ = self.make_transcriber(session)
        live.start()
        live.feed_audio(b"pcm")

        self.assertIsNone(live.finish(1.0))
        self.assertEqual(live.failure_reason, "empty_transcript")

    def test_interim_only_without_turn_complete_is_incomplete(self):
        session = FakeSession([server_message(interim="draft only")])
        live, _ = self.make_transcriber(session)
        live.start()
        live.feed_audio(b"pcm")

        self.assertIsNone(live.finish(1.0))
        self.assertTrue(live.failed)
        self.assertEqual(live.failure_reason, "incomplete_turn")

    def test_queue_overflow_invalidates_whole_live_result(self):
        gate = threading.Event()
        session = FakeSession(
            [server_message(final="partial", turn_complete=True)]
        )
        live, client = self.make_transcriber(
            session, connect_gate=gate, max_buffer_bytes=4
        )
        live.start()

        self.assertFalse(live.feed_audio(b"12345"))
        self.assertIsNone(live.finish(0.2))
        self.assertEqual(live.failure_reason, "buffer_overflow")
        self.assertTrue(wait_for(lambda: not live.alive))
        # Cancellation may happen before the factory is reached. If a client
        # was created, both of its owned transports must have been closed.
        if client.closed or client.aio.closed:
            self.assertTrue(client.closed)
            self.assertTrue(client.aio.closed)

    def test_finish_timeout_cancels_and_closes_owned_client_and_thread(self):
        session = FakeSession(hang=True)
        live, client = self.make_transcriber(session, receive_timeout=5.0)
        live.start()
        live.feed_audio(b"pcm")

        self.assertIsNone(live.finish(0.05))
        self.assertEqual(live.failure_reason, "finish_timeout")
        self.assertTrue(wait_for(lambda: not live.alive))
        self.assertTrue(client.connection.exited)
        self.assertTrue(client.aio.closed)
        self.assertTrue(client.closed)

    def test_cancel_during_connect_is_prompt_and_closes_created_client(self):
        gate = threading.Event()
        session = FakeSession()
        live, client = self.make_transcriber(session, connect_gate=gate)
        live.start()
        self.assertTrue(wait_for(lambda: bool(client.aio.live.calls)))

        started = time.monotonic()
        live.cancel()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(wait_for(lambda: not live.alive))
        self.assertEqual(live.failure_reason, "cancelled")
        self.assertTrue(client.aio.closed)
        self.assertTrue(client.closed)

    def test_finish_before_connection_flushes_all_queued_audio_in_order(self):
        gate = threading.Event()
        session = FakeSession(
            [server_message(final="ready", turn_complete=True)]
        )
        live, _ = self.make_transcriber(session, connect_gate=gate)
        live.start()
        self.assertTrue(live.feed_audio(b"one"))
        self.assertTrue(live.feed_audio(b"two"))

        result = []
        worker = threading.Thread(target=lambda: result.append(live.finish(1.0)))
        worker.start()
        time.sleep(0.02)
        gate.set()
        worker.join(1.0)

        self.assertEqual(result, ["ready"])
        audio = [call["audio"].data for call in session.sent if call.get("audio")]
        self.assertEqual(audio, [b"one", b"two"])
        end_index = next(
            index
            for index, call in enumerate(session.sent)
            if call.get("activity_end") is not None
        )
        audio_indices = [
            index
            for index, call in enumerate(session.sent)
            if call.get("audio") is not None
        ]
        self.assertTrue(all(index < end_index for index in audio_indices))

    def test_server_turn_complete_before_stop_is_invalid(self):
        session = FakeSession(
            [server_message(final="truncated", turn_complete=True)],
            early=True,
        )
        live, _ = self.make_transcriber(session)
        live.start()
        live.feed_audio(b"first")
        self.assertTrue(session.first_audio.wait(0.5))
        self.assertTrue(wait_for(lambda: not live.alive))

        self.assertIsNone(live.finish(0.2))
        self.assertEqual(live.failure_reason, "early_turn_complete")

    def test_generation_complete_before_stop_is_invalid(self):
        session = FakeSession(
            [server_message(final="truncated", generation_complete=True)],
            early=True,
        )
        live, _ = self.make_transcriber(session)
        live.start()
        live.feed_audio(b"first")
        self.assertTrue(session.first_audio.wait(0.5))
        self.assertTrue(wait_for(lambda: not live.alive))

        self.assertIsNone(live.finish(0.2))
        self.assertEqual(live.failure_reason, "early_turn_complete")

    def test_failure_reason_does_not_include_exception_message(self):
        class SecretFailure(RuntimeError):
            status_code = 503

        class FailingFactory:
            def __call__(self, api_key):
                raise SecretFailure("secret response and token")

        live = LiveTranscriber("test-key", client_factory=FailingFactory())
        live.start()
        self.assertIsNone(live.finish(1.0))
        self.assertEqual(live.failure_reason, "SecretFailure_503")
        self.assertNotIn("secret", live.failure_reason)


if __name__ == "__main__":
    unittest.main()
