from app.knowledge_engine import _tokens

q = "工作流引擎支持哪些节点"
qt = _tokens(q)
print("query tokens:", sorted(qt))
chunk_kw = {'2', '5', 'agent', 'agnes', 'ai', 'ceo', 'eos', 'flash', 'openai', '工作流'.decode() if False else '工作流'}
print("overlap with doc1 kw:", qt & {'2','5','agent','agnes','ai','ceo','eos','flash','openai'})
