"""Static file server with HTTP Range support - stands in for GitHub Pages,
and logs every byte range it serves so we can see what a query actually costs."""
import os, re, sys, threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

LOG = []

class H(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if not m: return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1); length = end - start + 1
        LOG.append((self.path, start, end, length))
        f = open(path, "rb"); f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        self.wfile.write(f.read(length)); f.close()
        return None

def serve(directory, port):
    os.chdir(directory)
    HTTPServer(("127.0.0.1", port), H).serve_forever()

if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]))
