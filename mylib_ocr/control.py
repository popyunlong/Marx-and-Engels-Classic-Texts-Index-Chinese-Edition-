"""Host-side audited admission controls, used only under the production lock."""
import argparse
import os
from pathlib import Path

from .queue import Queue


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path(os.environ.get('MYLIB_OCR_ROOT','/opt/mylib-ocr/shared')))
    commands=parser.add_subparsers(dest='command',required=True)
    grant=commands.add_parser('grant')
    grant.add_argument('--uid',type=int,required=True)
    grant.add_argument('--sid',type=int,required=True)
    grant.add_argument('--reason',required=True)
    dispatch=commands.add_parser('dispatch')
    dispatch.add_argument('state',choices=['on','off'])
    args=parser.parse_args()
    queue=Queue(args.root)
    if args.command=='grant':
        queue.grant(args.uid,args.sid,args.reason)
    else:
        queue.set_dispatch(args.state=='on')
    print('Audited OCR admission updated')


if __name__=='__main__':
    main()
