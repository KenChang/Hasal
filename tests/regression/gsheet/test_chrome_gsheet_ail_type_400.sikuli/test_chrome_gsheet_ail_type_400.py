# if you are putting your test script folders under {git project folder}/tests/, it will work fine.
# otherwise, you either add it to system path before you run or hard coded it in here.
sys.path.append(sys.argv[2])
import os
import gsheet
import common
import shutil
import browser
import time

# Disable Sikuli action and info log
com = common.General()
com.infolog_enable(0)

chrome = browser.Chrome()
gs = gsheet.gSheet()
chrome.clickBar()
chrome.enterLink(sys.argv[3])
gs.wait_for_loaded()

setAutoWaitTimeout(10)
sample2_fp = os.path.join(sys.argv[4], sys.argv[5].replace('sample_1', 'sample_2'))

sleep(5)
click(gs.gsheet_1st_cell)
sleep(2)
capture_width = int(sys.argv[6])
capture_height = int(sys.argv[7])

t1 = time.time()
capimg2 = capture(0, 0, capture_width, capture_height)

com.system_print('[log]  TYPE "9"')
type('9')
# In normal condition, a should appear within 100ms, but if lag happened, that could lead the show up after 100 ms, and that will cause the calculation of AIL much smaller than expected.
sleep(0.1)

t2 = time.time()
com.updateJson({'t1': t1, 't2': t2}, sys.argv[8])
shutil.move(capimg2, sample2_fp.replace(os.path.splitext(sample2_fp)[1], '.png'))