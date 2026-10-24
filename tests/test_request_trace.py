"""Real installed Pi against a local streaming stub; no provider calls or quality claim."""
import asyncio
import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_report_agent.local_models import resolve_model_selection
from video_report_agent.pi import PiRunner


@pytest.mark.skipif(shutil.which('pi') is None, reason='Pi is not installed')
def test_pi_request_hook_measures_first_nonempty_stream_content(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            Handler.calls += 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def send(delta, finish=None, usage=None):
                data = {'id': 'stub', 'object': 'chat.completion.chunk', 'created': 1,
                        'model': 'trace-stub', 'choices': [
                            {'index': 0, 'delta': delta, 'finish_reason': finish}]}
                if usage:
                    data['usage'] = usage
                self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                self.wfile.flush()

            send({'role': 'assistant', 'content': ''})
            time.sleep(0.08)
            if Handler.calls == 1:
                send({'tool_calls': [{'index': 0, 'id': 'write-report', 'type': 'function',
                                     'function': {'name': 'write', 'arguments': json.dumps({
                                         'path': 'report.html',
                                         'content': '<html><body>local fixture</body></html>'})}}]})
                reason = 'tool_calls'
            else:
                send({'content': 'Done'})
                reason = 'stop'
            send({}, reason, {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30})
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'models.json').write_text(json.dumps({'providers': {'trace-local': {
        'baseUrl': f'http://127.0.0.1:{server.server_port}/v1',
        'api': 'openai-completions', 'apiKey': 'local-only',
        'models': [{'id': 'trace-stub', 'name': 'Trace stub', 'reasoning': False,
                    'input': ['text'], 'contextWindow': 128000, 'maxTokens': 4096,
                    'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}]
    }}}))
    monkeypatch.setattr('video_report_agent.pi.PI_AGENT_DIR', config)
    monkeypatch.setenv('PI_TRACE_FULL', '1')
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'transcript.md').write_text('Local timing fixture')
    try:
        result = asyncio.run(PiRunner(provider='trace-local', model='trace-stub',
                                      api_key='local-only', thinking='off', timeout=30,
                                      isolation='local').run(run))
        assert 'local fixture' in result.read_text()
        events = [json.loads(line) for line in (run / 'pi.events.jsonl').read_text().splitlines()]
        starts = [e for e in events if e['type'] == 'request_start']
        first = [e for e in events if e['type'] == 'request_first_response']
        assert len(starts) == len(first) == 2
        assert all(e['input_bytes'] > 0 for e in starts)
        assert [e['request_id'] for e in first] == [e['request_id'] for e in starts]
        assert all(50 <= e['latency_ms'] < 10000 for e in first)
        assert [e['response_kind'] for e in first] == ['toolcall_delta', 'text_delta']
        assert events[-1]['type'] == 'agent_settled'
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.skipif(shutil.which('pi') is None, reason='Pi is not installed')
def test_local_provider_discovers_mock_model_and_generates_report_through_pi(
    tmp_path, monkeypatch
):
    class Handler(BaseHTTPRequestHandler):
        model_requests = []
        completion_requests = []

        def log_message(self, *args):
            pass

        def do_GET(self):
            Handler.model_requests.append(self.path)
            payload = json.dumps({"data": [{"id": "mock-runtime-model"}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            Handler.completion_requests.append(body)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def send(delta, finish=None, usage=None):
                data = {'id': 'mock', 'object': 'chat.completion.chunk', 'created': 1,
                        'model': body['model'], 'choices': [
                            {'index': 0, 'delta': delta, 'finish_reason': finish}]}
                if usage:
                    data['usage'] = usage
                self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                self.wfile.flush()

            if len(Handler.completion_requests) == 1:
                send({'role': 'assistant', 'content': ''})
                send({'tool_calls': [{'index': 0, 'id': 'write-local-report', 'type': 'function',
                                      'function': {'name': 'write', 'arguments': json.dumps({
                                          'path': 'report.html',
                                          'content': '<html><body>mock local report</body></html>'
                                      })}}]})
                finish = 'tool_calls'
            else:
                send({'role': 'assistant', 'content': 'Report written.'})
                finish = 'stop'
            send({}, finish, {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30})
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = tmp_path / 'pi-config'
    monkeypatch.setattr('video_report_agent.pi.PI_AGENT_DIR', config)
    monkeypatch.setenv('PI_PROVIDER', 'local')
    monkeypatch.setenv('PI_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('PI_API_KEY', 'temporary-mock-key')
    monkeypatch.setenv('PI_TRACE_FULL', '1')
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'transcript.md').write_text('Local mock transcript')
    try:
        selection = resolve_model_selection()
        result = asyncio.run(PiRunner(
            provider=selection['provider'], model=selection['model'], thinking='off',
            timeout=30, isolation='local'
        ).run(run))

        saved_provider = json.loads((config / 'models.json').read_text())[
            'providers'
        ]['local']
        events = [json.loads(line) for line in (run / 'pi.events.jsonl').read_text().splitlines()]
        assistant_ends = [event['message'] for event in events
                          if event['type'] == 'message_end'
                          and event.get('message', {}).get('role') == 'assistant']
        settled_positions = [index for index, event in enumerate(events)
                             if event['type'] == 'agent_settled']

        assert 'mock local report' in result.read_text()
        assert Handler.model_requests == ['/v1/models']
        assert saved_provider['models'][0]['id'] == 'mock-runtime-model'
        assert saved_provider['baseUrl'] == f'http://127.0.0.1:{server.server_port}/v1'
        assert len(Handler.completion_requests) == 2
        assert all(request['model'] == 'mock-runtime-model'
                   for request in Handler.completion_requests)
        assert assistant_ends[-1]['stopReason'] == 'stop'
        assert settled_positions == [len(events) - 1]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
