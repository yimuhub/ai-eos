#!/bin/bash
# 完整 E2E 回归：清理会议/工厂数据后按序跑 9 个测试
cd ~/Documents/张阗/ai-eos/apps/api
docker exec ai-eos-postgres psql -U aieos -d aieos -c "DELETE FROM agent_factory_jobs WHERE created_at >= CURRENT_DATE; DELETE FROM meetings; DELETE FROM meeting_messages; DELETE FROM meeting_participants; DELETE FROM meeting_action_items;" > /dev/null 2>&1

cd ~/Documents/张阗/ai-eos
PASS=0; FAIL=0
for t in tests/test_p301_e2e.py tests/test_p302_e2e.py tests/test_p304_e2e.py tests/test_p305_306_e2e.py tests/test_p307_e2e.py tests/test_p308_e2e.py tests/test_p310_311_e2e.py tests/test_p312_e2e.py tests/test_p313_316_e2e.py; do
  if python3 $t > /tmp/r.txt 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    echo "FAIL $t: $(grep '❌' /tmp/r.txt | head -1)"
  fi
done
echo "== E2E: $PASS pass / $FAIL fail =="

python3 tests/test_p3_final_integration.py > /tmp/final.txt 2>&1
if [ $? -eq 0 ]; then
  echo "FINAL: $(grep '通过' /tmp/final.txt | tail -1)"
else
  echo "FINAL FAILED"
  grep '❌' /tmp/final.txt | head -5
fi
