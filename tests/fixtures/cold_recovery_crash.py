"""Controlled crash only: actual worker lifetime locks, real claim/checkpoint."""
import os
import sys
from model_generator.web.jobs import JobRepository
from model_generator.web.worker import main
mode=sys.argv[1]
if mode not in {'claim','checkpoint'}:raise SystemExit('Explicit test crash boundary required')
original=getattr(JobRepository,'claim_next' if mode=='claim' else 'checkpoint')
def crash(self,*args,**kwargs):
    result=original(self,*args,**kwargs)
    if mode=='checkpoint' or result is not None:os._exit(137)
    return result
setattr(JobRepository,'claim_next' if mode=='claim' else 'checkpoint',crash)
sys.argv=['worker']
main()
