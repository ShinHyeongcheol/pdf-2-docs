"""JSON-lines host MCP session. The host supplies connected tool responses on stdin."""
import argparse
import json
import sys
import termios
from pathlib import Path
from uuid import uuid4
from .notion_bridge import BridgeSpec,run_connected
from .rag_files import read_json_input


class StdioMcpGateway:
    mode='host_connected_mcp'
    def call(self,tool,arguments):
        if tool not in {'notion_fetch','notion_update_page'}:raise ValueError('unsupported host action')
        request_id=str(uuid4())
        print(json.dumps(dict(type='mcp_request',id=request_id,tool=tool,arguments=arguments),ensure_ascii=False),flush=True)
        line=sys.stdin.readline(2_000_001)
        if not line.endswith('\n') or len(line.encode())>2_000_000:raise ValueError('complete bounded host response required')
        response=json.loads(line)
        if set(response)!={'id','packet'} or response['id']!=request_id or not isinstance(response['packet'],dict):
            raise ValueError('host response identity mismatch')
        return response['packet']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv);terminal=None
    try:
        if sys.stdin.isatty():
            terminal=termios.tcgetattr(sys.stdin.fileno());settings=terminal.copy()
            settings[3]&=~(termios.ECHO|termios.ICANON);termios.tcsetattr(sys.stdin.fileno(),termios.TCSANOW,settings)
        spec=BridgeSpec.model_validate(read_json_input(args.spec))
        result=run_connected(spec,output_dir=args.output_dir,gateway=StdioMcpGateway())
        print(json.dumps(dict(type='result',result=result),ensure_ascii=False),flush=True)
    except Exception as exc:
        print(json.dumps(dict(type='blocked',error=type(exc).__name__)),flush=True)
        raise SystemExit(1) from None
    finally:
        if terminal is not None:termios.tcsetattr(sys.stdin.fileno(),termios.TCSANOW,terminal)


if __name__=='__main__':main()
