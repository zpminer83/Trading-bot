import os
import runpy

os.environ['PAPER_LOOP_ITERATIONS'] = '120'
os.environ['PAPER_LOOP_INTERVAL_SECONDS'] = '10'
os.environ['PAPER_RUN_FSYNC'] = 'false'
os.environ['PAPER_SIGNAL_ENABLED'] = 'true'
os.environ['PAPER_FAIR_PLAY_ENABLED'] = 'true'
os.environ['PAPER_MAX_DRAWDOWN_RATIO'] = '0.10'

runpy.run_module('scripts.run_rest_paper_loop', run_name='__main__')
