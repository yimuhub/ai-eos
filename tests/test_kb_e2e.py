"""P2-06 KB e2e v2 - URL-encode query params properly."""
import json
import urllib.parse
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("/tmp/aieos_token").read().strip()
CID = open("/tmp/aieos_cid").read().strip()


def api(method, path, params=None, data=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}")
        raise


DOC1 = """AI-EOS 是一套 AI 企业操作系统，核心目标是让多个 AI Agent 像真实公司一样协同工作。

系统采用 公司-部门-Agent 三层架构。CEO Agent 负责拆解目标并分派任务，各部门 Agent 并行执行后汇总成果。

模型网关支持 OpenAI 兼容接口，可接入 agnes-2.5-flash 等任意第三方模型。每个 Agent 可配置主备模型链，失败自动降级。

工作流引擎支持顺序、并行、条件、等待、审批等节点类型，所有状态持久化到数据库，重启不丢失。"""

DOC2 = """文旅项目策划方法论：策划分为资源盘点、市场分析、定位策划、产品体系、投资测算五个阶段。

资源盘点要摸清景区的自然资源、文化资源、区位交通三大要素。市场分析重点关注客源结构、消费能力、竞品分布。

投资测算采用动态回收期法，一般文旅项目合理回收期为 8-12 年，重资产项目不超过 15 年。"""

print("=== P2-06 Knowledge Base ===")

d1 = api("POST", "/knowledge/documents", {"company_id": CID},
         {"title": "AI-EOS 系统白皮书", "content": DOC1})
d2 = api("POST", "/knowledge/documents", {"company_id": CID},
         {"title": "文旅策划方法论", "content": DOC2})
print(f"[1] 入库: {d1['title']}({d1['chunk_count']}块) | {d2['title']}({d2['chunk_count']}块)")

docs = api("GET", "/knowledge/documents", {"company_id": CID})
print(f"[2] 文档列表: {len(docs)} 份")

r1 = api("GET", "/knowledge/search",
         {"company_id": CID, "q": "工作流引擎支持哪些节点", "top_k": 3})
assert r1["results"], "检索无结果!"
print(f"[3] 检索'工作流引擎': score={r1['results'][0]['score']} 片段: {r1['results'][0]['content'][:40]}...")

r2 = api("GET", "/knowledge/search",
         {"company_id": CID, "q": "投资测算 回收期", "top_k": 3})
assert r2["results"], "检索无结果!"
print(f"[4] 检索'投资测算': 片段: {r2['results'][0]['content'][:40]}...")

rag = api("GET", "/knowledge/rag",
          {"company_id": CID, "q": "AI-EOS 模型网关怎么接模型", "top_k": 2})
assert rag["context"], "RAG 无上下文!"
print(f"[5] RAG ({len(rag['context'])}字): {rag['context'][:50]}...")

detail = api("GET", f"/knowledge/documents/{d2['id']}")
print(f"[6] 详情: {detail['title']} {detail['chunk_count']} 块")

api("DELETE", f"/knowledge/documents/{d2['id']}")
docs_after = api("GET", "/knowledge/documents", {"company_id": CID})
assert len(docs_after) == len(docs) - 1
print(f"[7] 删除 OK，剩余 {len(docs_after)} 份")

print("\n=== P2-06 E2E PASSED ===")
