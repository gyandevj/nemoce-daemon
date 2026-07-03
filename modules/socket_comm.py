import socket
import json

def send_msg(sock: socket.socket, data: dict):
    """
    Serialize dict to JSON, append a newline delimiter, and send over socket.
    """
    payload = json.dumps(data) + "\n"
    sock.sendall(payload.encode("utf-8"))

def recv_msg(sock: socket.socket) -> dict:
    """
    Read from socket byte-by-byte until a newline delimiter is found,
    then decode and parse as a JSON dictionary. Returns None on EOF.
    """
    buffer = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            # Connection closed/EOF
            return None
        if chunk == b'\n':
            break
        buffer.extend(chunk)
    
    if not buffer:
        return {}
        
    return json.loads(buffer.decode("utf-8"))
