import base64
import os
import select
import socket
import sys

HOST = sys.argv[1]
PORT = int(sys.argv[2])
PROXY = ('10.16.1.51', 3128)
AUTH = 'preset:6e298f07'

s = socket.create_connection(PROXY, 15)
hdr = ('CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n'
       'Proxy-Authorization: Basic %s\r\n\r\n'
       % (HOST, PORT, HOST, PORT,
          base64.b64encode(AUTH.encode()).decode()))
s.sendall(hdr.encode())
resp = b''
while b'\r\n\r\n' not in resp:
    d = s.recv(4096)
    if not d:
        break
    resp += d
line = resp.split(b'\r\n')[0]
if b'200' not in line:
    sys.stderr.write(line.decode(errors='replace') + '\n')
    sys.exit(1)

while True:
    r, _, _ = select.select([0, s], [], [])
    if 0 in r:
        d = os.read(0, 65536)
        if not d:
            break
        s.sendall(d)
    if s in r:
        d = s.recv(65536)
        if not d:
            break
        os.write(1, d)
s.close()