# AI-EOS 备份恢复手册

> 原则：先备份，再动手。任何删库、升级、迁移操作之前，先走一遍本手册。
> `packaging/打包说明.md` 故障排查里的 `rm -f data/aieos.db` 已改为"先备份再重建"。

## 1. 要备份什么

| 内容 | 位置 | 说明 |
|------|------|------|
| 数据库 | PostgreSQL：`pgdata` 卷；SQLite：`data/aieos.db` | 全部业务数据 |
| 文件产出物 | `storage/`（上传、成果、知识库原文、截屏） | 丢了 DB 可重建、丢了 storage 不可 |
| 配置密钥 | `apps/api/.env`、`docker/.env` | 含密钥，离线单独保管，切勿与备份放同一台机器 |
| 日志（可选） | `logs/`、`/var/log/ai-eos` | 排障用，非必须 |

## 2. SQLite（便携包）备份

```bash
# 停服后做文件级拷贝（最稳；WAL 模式下 -wal/-shm 会一并拷走）
cp data/aieos.db "data/aieos.db.bak-$(date +%Y%m%d-%H%M%S)"

# 若偏好在线热备（不停服），先 checkpoint 再拷贝：
sqlite3 data/aieos.db "PRAGMA wal_checkpoint(TRUNCATE);"
cp data/aieos.db /path/to/backup/

# storage 同步
rsync -a --delete storage/ /path/to/backup/storage/
```

> 启动器（`start-mac.sh` / `start-win.bat`）每次启动前会自动快照 `data/`
> 为 `data-backup-YYYYMMDD-HHMMSS`，只保留最近 5 份——这是兜底，不是备份策略。

## 3. PostgreSQL（Docker）备份

```bash
cd docker
# 逻辑备份（推荐每日）：
docker compose exec -T postgres pg_dump -U aieos aieos | gzip > "backup/aieos-$(date +%Y%m%d).sql.gz"

# 卷级快照（大库/快速恢复）：
docker run --rm -v ai-eos_pgdata:/data -v "$(pwd)/backup:/backup" \
  alpine tar czf "/backup/pgdata-$(date +%Y%m%d).tar.gz" -C /data .

# storage 卷同样打包：
docker run --rm -v ai-eos_api-storage:/data -v "$(pwd)/backup:/backup" \
  alpine tar czf "/backup/storage-$(date +%Y%m%d).tar.gz" -C /data .
```

定时任务示例（每天 03:00，保留 7 天）：

```cron
0 3 * * * cd /opt/ai-eos/docker && ./backup-pg.sh && find backup -name 'aieos-*.sql.gz' -mtime +7 -delete
```

## 4. 恢复步骤

### SQLite
```bash
# 1. 停服  2. 把备份拷回 3. 启动
cp /path/to/backup/aieos.db data/aieos.db
rsync -a /path/to/backup/storage/ storage/
```

### PostgreSQL
```bash
cd docker
# 1. 停 api（防写入）
docker compose stop api web
# 2. 恢复
zcat backup/aieos-20261014.sql.gz | docker compose exec -T postgres psql -U aieos -d aieos
# 3. 恢复 storage 卷（如有）
docker run --rm -v ai-eos_api-storage:/data -v "$(pwd)/backup:/backup" \
  alpine tar xzf /backup/storage-20261014.tar.gz -C /data
# 4. 启动并冒烟
docker compose up -d
curl -f http://localhost:8001/health && curl -f http://localhost:8001/health/db
```

## 5. 恢复后冒烟清单

1. `GET /health` → 200 `{"status":"ok"}`
2. `GET /health/db` → 200（非 503）
3. 登录页可正常登录，指挥中心加载无报错
4. 抽查一条历史任务/成果可打开
5. 检查日志无连续 ERROR：`tail -100 logs/ai-eos.log`

## 6. 保留策略建议

- 每日 1 份，保留 7 天；每周 1 份，保留 4 周；每月 1 份，保留 3 个月。
- 备份与生产不在同一台机器；密钥文件（`.env`）单独离线保管。
- 每季度做一次恢复演练（用备份在测试环境完整恢复一遍），只备不练等于没备。
