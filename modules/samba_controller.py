import subprocess
import logging
import json
import re

logger = logging.getLogger("lab-daemon")

class SambaController:
    """
    Inspects Samba server status using 'smbstatus' to track open file handles and connections.
    """
    def __init__(self, sessions_path: str = "/tmp/labdata/sessions"):
        self.sessions_path = sessions_path

    def _run_smbstatus(self, arg: str) -> str:
        try:
            # Run: smbstatus -L or -S
            res = subprocess.run(["smbstatus", arg], capture_output=True, text=True, check=True)
            return res.stdout
        except FileNotFoundError:
            logger.debug("smbstatus executable not found. Mocking empty status.")
            return ""
        except subprocess.CalledProcessError as e:
            logger.debug(f"smbstatus {arg} failed: {e.stderr.strip()}")
            return ""
        except Exception as e:
            logger.debug(f"Unexpected error running smbstatus {arg}: {e}")
            return ""

    def get_open_handles(self, machine_id: str) -> list[dict]:
        """
        Returns a list of open file handles under the session path for the specified machine.
        Each handle is a dict: {"pid": str, "path": str, "access": str, "name": str}
        """
        output = self._run_smbstatus("-L")
        handles = []
        if not output:
            return handles

        # We want to find files opened under the machine's session folder, e.g., /tmp/labdata/sessions/microscope1/
        machine_session_path = f"{self.sessions_path}/{machine_id.lower()}"
        
        # Try JSON parsing if Samba supports it (smbstatus -L -j is supported in newer Samba versions)
        # However, standard smbstatus -L is text. Let's parse text.
        # Format of smbstatus -L:
        # Pid          Uid        DenyMode   Access      R/W        Oplock          SharePath   Name   Time
        # --------------------------------------------------------------------------------------------------
        # 12345        65534      DENY_NONE  0x20089     RDONLY     NONE            /tmp/labdata/sessions/microscope1   my_files/image.tif   Wed Jun 17 14:00:00 2026
        
        lines = output.strip().split("\n")
        header_index = -1
        for i, line in enumerate(lines):
            if "Locked files:" in line or "Pid" in line and "SharePath" in line:
                header_index = i
                break
                
        if header_index == -1:
            # Check if JSON is returned
            try:
                data = json.loads(output)
                if isinstance(data, dict) and "open_files" in data:
                    for item in data["open_files"].values():
                        spath = item.get("sharepath", "")
                        if machine_session_path in spath or spath in machine_session_path:
                            handles.append({
                                "pid": str(item.get("pid", "")),
                                "path": os.path.join(spath, item.get("name", "")),
                                "access": "RDWR" if "RDWR" in item.get("access", "") or "WRONLY" in item.get("access", "") else "RDONLY",
                                "name": item.get("name", "")
                            })
                    return handles
            except Exception:
                pass
            return handles

        # Process lines after the header divider (lines of dashes)
        start_processing = False
        for line in lines[header_index + 1:]:
            line = line.strip()
            if not line:
                continue
            if line.startswith("---"):
                start_processing = True
                continue
            if not start_processing:
                continue
            
            # Parse row. Use regex to handle potential spaces in filenames
            # Columns: Pid, Uid, DenyMode, Access, R/W, Oplock, SharePath, Name, Time...
            # A typical line might look like:
            # 2259163 65534      DENY_NONE  0x100081    RDONLY     NONE             /tmp/labdata/sessions/microscope1   my_files/image.tif   Wed Jun 17 14:00:00 2026
            parts = re.split(r'\s{2,}', line)
            if len(parts) >= 8:
                try:
                    pid = parts[0].strip()
                    rw = parts[4].strip()  # "RDONLY" or "RDWR"
                    sharepath = parts[6].strip()
                    name = parts[7].strip()
                    
                    if machine_session_path in sharepath or sharepath in machine_session_path:
                        full_path = os.path.join(sharepath, name)
                        handles.append({
                            "pid": pid,
                            "path": full_path,
                            "access": "RDWR" if "RDWR" in rw or "WRONLY" in rw else "RDONLY",
                            "name": name
                        })
                except Exception as e:
                    logger.debug(f"SambaController parsing error on row '{line}': {e}")
                    
        return handles

    def get_connected_clients(self, machine_id: str) -> list[str]:
        """
        Returns a list of IP addresses currently connected to the machine's share.
        """
        output = self._run_smbstatus("-S")
        clients = []
        if not output:
            return clients

        # Format of smbstatus -S:
        # Service      pid     Machine       Connected at
        # -------------------------------------------------------
        # microscope1  12345   192.168.1.10  Wed Jun 17 14:00:00 2026
        
        lines = output.strip().split("\n")
        start_processing = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if "Service" in line and "pid" in line:
                continue
            if line.startswith("---"):
                start_processing = True
                continue
            if not start_processing:
                continue
                
            parts = line.split()
            if len(parts) >= 3:
                service = parts[0]
                client_ip = parts[2]
                if service == machine_id:
                    clients.append(client_ip)
                    
        return list(set(clients))
