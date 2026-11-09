"""Local HTTP server and launcher for the Engineering Design Notes app (loopback only, per-launch token)."""
import argparse
import json
import os
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from designnotes.service import NotesService, default_root
from designnotes.transcript import TranscriptError

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / 'static'
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/styles.css': ('styles.css', 'text/css; charset=utf-8')}
MAX_JSON = 2 * 1024 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def safe_filename(title, extension):
    stem = re.sub(r'[^\w\- ]+', '', title or 'engineering-notes').strip().replace(' ', '_')[:80] or 'engineering-notes'
    return f'{stem}.{extension}'


def handler_factory(service, token, allowed_hosts=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'DesignNotes'

        def log_message(self, *args):
            pass

        def respond(self, status, data, mime='application/json', filename=None):
            body = json.dumps(data).encode() if mime == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', CSP)
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def safe_host(self):
            hosts = allowed_hosts or {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host')
            origin = self.headers.get('Origin')
            return host in hosts and (origin is None or origin in {'http://' + h for h in hosts} | {'https://' + h for h in hosts})

        def authorized(self):
            return secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def guard(self):
            if not self.safe_host():
                self.respond(403, {'error': 'Invalid host or origin.'})
                return False
            if not self.authorized():
                self.respond(401, {'error': 'Open the app using the link printed by the launcher.'})
                return False
            return True

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
                return
            if path in ASSETS:
                if not self.safe_host():
                    self.respond(403, {'error': 'Invalid host or origin.'})
                    return
                name, mime = ASSETS[path]
                self.respond(200, (STATIC / name).read_bytes(), mime)
                return
            if not self.guard():
                return
            parts = path.strip('/').split('/')
            try:
                if path == '/api/status':
                    self.respond(200, service.status())
                elif path == '/api/notes':
                    self.respond(200, service.list())
                elif len(parts) == 3 and parts[:2] == ['api', 'notes']:
                    self.respond(200, service.snapshot(parts[2]))
                elif len(parts) == 4 and parts[:2] == ['api', 'notes'] and parts[3] in ('export.md', 'export.pdf'):
                    record = service.snapshot(parts[2])
                    if parts[3] == 'export.md':
                        self.respond(200, service.export_markdown(parts[2]).encode('utf-8'), 'text/markdown; charset=utf-8',
                                     safe_filename(record['title'], 'md'))
                    else:
                        self.respond(200, service.export_pdf(parts[2]), 'application/pdf', safe_filename(record['title'], 'pdf'))
                else:
                    self.respond(404, {'error': 'Not found.'})
            except KeyError:
                self.respond(404, {'error': 'Notes not found.'})
            except ValueError as error:
                self.respond(400, {'error': str(error)})
            except Exception:
                self.respond(500, {'error': 'Local service error.'})

        def read_json(self):
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_JSON or not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ValueError('Expected a JSON request of at most 2 MB.')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object.')
            return body

        def do_POST(self):
            if not self.guard():
                return
            path = urlparse(self.path).path
            parts = path.strip('/').split('/')
            try:
                if path == '/api/upload':
                    length = int(self.headers.get('Content-Length', '0'))
                    header = lambda name: unquote(self.headers.get(name, '')) or None
                    identifier = service.create_from_upload(header('X-Filename'), self.rfile, length, header('X-Title'),
                                                            header('X-Project'), header('X-Glossary'))
                    self.respond(202, {'id': identifier})
                    return
                body = self.read_json()
                if path == '/api/key':
                    service.set_key(body.get('key'))
                    self.respond(200, {'ok': True})
                elif path == '/api/paste':
                    identifier = service.create_from_text(body.get('text'), body.get('title'), body.get('project'))
                    self.respond(202, {'id': identifier})
                elif path == '/api/quit':
                    self.respond(200, {'ok': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif len(parts) == 4 and parts[:2] == ['api', 'notes']:
                    identifier, operation = parts[2], parts[3]
                    if operation == 'edit':
                        service.edit(identifier, body.get('section'), body.get('item_id'), body.get('text'),
                                     body.get('delete') is True)
                    elif operation == 'rename':
                        service.rename(identifier, body.get('title'), body.get('project'))
                    elif operation == 'approve':
                        service.approve(identifier, body.get('approved') is not False)
                    elif operation == 'retry':
                        service.retry(identifier)
                    elif operation == 'delete':
                        service.delete(identifier)
                        self.respond(200, {'ok': True})
                        return
                    else:
                        self.respond(404, {'error': 'Unknown operation.'})
                        return
                    self.respond(200, service.snapshot(identifier))
                else:
                    self.respond(404, {'error': 'Unknown operation.'})
            except json.JSONDecodeError:
                self.respond(400, {'error': 'Malformed JSON request.'})
            except KeyError:
                self.respond(404, {'error': 'Notes not found.'})
            except (TranscriptError, ValueError, TypeError) as error:
                self.respond(400, {'error': str(error) or 'Invalid request.'})
            except Exception:
                self.respond(500, {'error': 'Operation failed. Uploaded data is kept where available.'})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description='Engineering Design Notes - local browser app')
    parser.add_argument('--port', type=int, default=0, help='Port to listen on (default: random free port)')
    parser.add_argument('--no-browser', action='store_true', help='Do not open a browser window')
    parser.add_argument('--data-dir', help='Where notes are stored (default: per-user app data folder)')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    service = NotesService(Path(args.data_dir) if args.data_dir else default_root())
    token = os.getenv('DESIGNNOTES_TOKEN') or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_factory(service, token))
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}/#{token}'
    print(f'Engineering Design Notes is running at {url}')
    print(f'Data folder: {service.root}')
    print('Press Ctrl+C or use "Quit" in the app to stop.')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    sys.exit(main())
