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

## 6. 代码仓库双份备份（本地 + 云端）

数据库和 `.env` 由 `tools/backup.sh` 负责；**代码**由 `tools/backup_repo.sh` 负责。
一份代码备份 = git bundle（已提交的全部历史）+ 工作区快照（尚未提交的改动），
再加同目录下的数据库备份。

```bash
# 本地快照（默认写到仓库同级 ../aieos-backups/repo，保留最近 14 份）
./tools/backup_repo.sh
```

**云端就是 GitHub**：每次 `git push` 后，代码已经在 `origin/main` 上，这就是异地那一份。
本机这份快照只解决一个 GitHub 管不到的问题——**还没提交的改动**（`git status` 里那些
`M` / `??` 文件），防止本机突然坏掉时丢掉正在写的代码。

> 如果你还想额外放一份到 iCloud / 外置硬盘，脚本支持先加密再落盘：
> `AIEOS_BACKUP_PASSPHRASE='自己定的长口令' ./tools/backup_repo.sh --encrypt --out <目标目录>`。
> 这里的"口令"就是**加密密码**，只在这条命令里用；不用 iCloud 就完全不需要管它。
> 用了就必须记在密码管理器里——丢了口令也就打不开备份了。

### 6.1 必备两份，第三份可选

| # | 位置 | 内容 | 频率 | 谁在维护 |
|---|------|------|------|----------|
| 1 | GitHub 私有仓库 `origin/main` | 已提交的代码 + 标签 + Release 说明 | 每次提交 / 每天 | 开发者手动 push |
| 2 | 本机 `../aieos-backups/repo` | bundle + 工作区快照 + 数据库 | 每日自动（launchd） | `tools/backup_repo.sh` |
| 3 | iCloud / OneDrive / 外置硬盘（可选） | 加密后的第 2 份拷贝 | 每日自动 | 系统同步 / 手动 rsync |

GitHub 解决"本机坏了"；本地快照解决"GitHub 挂了 / 误 force-push / 改动还没提交"；
第 3 份是给"本机硬盘和 GitHub 同时出问题"兜底的，不是必须。

### 6.2 每日自动备份（macOS launchd）

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/dev.aieos.backup.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.aieos.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$HOME/Documents/张阗/ai-eos" &amp;&amp; ./tools/backup_repo.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/aieos-backup.log</string>
  <key>StandardErrorPath</key><string>/tmp/aieos-backup.err</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/dev.aieos.backup.plist
```

需要同时上云时，把 `ProgramArguments` 里那条命令换成带 `AIEOS_BACKUP_PASSPHRASE`
和 `--encrypt --out <iCloud 目录>` 的版本（口令建议从 `~/.config/aieos/backup.env`
读取，该文件权限设 `600`，且不要放进任何被 git 跟踪的目录）。

### 6.3 从备份恢复代码

```bash
# 方式一：从 bundle 完整重建（含所有分支和标签）
git clone aieos-repo-YYYYmmdd-HHMMSS-<host>.bundle ai-eos-restored
cd ai-eos-restored && git remote set-url origin <GitHub 地址> && git push -u origin --all

# 方式二：只找回未提交的改动（解开工作区快照）
mkdir /tmp/wt && tar -xzf aieos-repo-...-worktree.tar.gz -C /tmp/wt
# 然后把 /tmp/wt 与当前仓库对比，挑回需要的文件

# 加密产物先解密
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -pass env:AIEOS_BACKUP_PASSPHRASE \
  -in aieos-repo-....bundle.enc -out aieos-repo-....bundle

# 数据库 / .env 从 tools/restore.sh 恢复（见第 4 节）
```

恢复后先跑 `/health`、`/health/db` 与 `./tools/check.sh --fast`，确认代码与数据都可用。

## 7. 保留策略建议

- 每日 1 份，保留 7 天；每周 1 份，保留 4 周；每月 1 份，保留 3 个月。
- 备份与生产不在同一台机器；密钥文件（`.env`）单独离线保管。
- 每季度做一次恢复演练（用备份在测试环境完整恢复一遍），只备不练等于没备。
