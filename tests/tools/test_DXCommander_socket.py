import socket
import time
def send_commander_command(command: str, host: str = '127.0.0.1', port: int = 7374) -> None:
    """Sends a TCP directive to DX Lab Suite Commander."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))
            s.sendall(command.encode('utf-8'))
            print(f"Sent: {command}")
            # time.sleep(0.5)  # Allow time for Commander to process the command
            # try:
            #     response = s.recv(1024)
            #     if response:
            #         print(f"Commander response: {response.decode('utf-8').strip()}")
            #     else:
            #         print("No response received (Commander closed connection).")
            # except socket.timeout:
            #     print("Read timed out — Commander accepted command but sent no reply.")
    except Exception as e:
        print(f"Connection error: {e}")


def query_commander(command, host='127.0.0.1', port=7374, buffer_size=1024):
    """Sends a query to DX Lab Commander and returns the response."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
            # Send query command
            s.sendall(command.encode('utf-8'))
            print(f"Sent query: {command}")
            # Read the response back from Commander
            response = s.recv(buffer_size)
            return response.decode('utf-8').strip()
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    # Query current VFO Frequency
    # current_freq = query_commander("<CmdVFO:1>14.074")
    # print(f"Current Frequency: {current_freq}")
    
    # # Query current Operating Mode
    # current_mode = query_commander("<command:11>CmdSendMode<parameters:0>")
    # print(f"Current Mode: {current_mode}")

    # exit()
    freq_command = "<command:14>CmdSetFreqMode<parameters:56><xcvrfreq:7>14020.53<xcvrmode:2>CW<preservesplitanddual:1>N"
    # Command to set Mode to USB
    mode_command = "<command:10>CmdSetMode<parameters:7><1:2>CW"
    # mode_command = "<command:10>CmdSetMode<parameters:7><1:4>Data"

    # Send commands sequentially
    print("Query frequency: ", query_commander("<command:10>CmdGetFreq<parameters:0>"))
    print("Query Mode: ", query_commander("<command:11>CmdSendMode<parameters:0>"))
    
    send_commander_command(freq_command)
    send_commander_command(mode_command)
    time.sleep(0.1)
    print("Query freq 2: ", query_commander("<command:10>CmdGetFreq<parameters:0>"))
    print("Query Mode 2: ", query_commander("<command:11>CmdSendMode<parameters:0>"))
    