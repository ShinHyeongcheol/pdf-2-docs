"""Replay existing approved artifacts locally; never loads an API key or calls providers."""
import argparse
import json
from pathlib import Path
from .offline_replay import ReplaySpec, run_replay
from .rag_files import read_json_input


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args = parser.parse_args(argv)
    try:
        result = run_replay(ReplaySpec.model_validate(read_json_input(args.spec,max_bytes=2_000_000)),output_dir=args.output_dir)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        parser.exit(1,'offline_replay_blocked:'+type(exc).__name__+'\n')


if __name__ == '__main__': main()
