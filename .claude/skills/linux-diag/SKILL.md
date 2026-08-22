---
name: linux-diag
description: Linux系OSのシステム不具合診断スキル。システムが遅い、応答しない、エラーが発生する等の問題調査時に使用。CPU、メモリ、ディスク、ネットワーク、プロセス、サービス、ログ、GPU/NVIDIA、グラフィック/xrdp、Docker/コンテナの診断コマンドとフローチャートを提供。
---

# Linux System Diagnostics Skill

Linux系OSのシステム不具合を調査・診断するための知識ベースです。

Linux 専用。`systemctl`、`journalctl`、`ss`、`lsblk` などを利用できる環境を前提とする。

## 概要

このスキルは以下の状況で自動的に参照されます：

- システムが遅い・重い
- サーバーが応答しない
- プロセスがハングしている
- ディスク容量やI/O問題
- ネットワーク接続問題
- サービスが起動しない
- 原因不明のエラー
- GPU/NVIDIAの問題（nvidia-smi失敗、CUDA/NVEncエラー）
- グラフィック/ディスプレイ問題（xrdp黒画面、DRM権限エラー）
- Docker/コンテナの問題（コンテナ内からデバイスアクセス不可）
- ユーザー権限/グループの問題（デバイスアクセス拒否）

## 診断カテゴリ

| カテゴリ | 主な症状 | 詳細 |
|---------|---------|------|
| CPU | 高負荷、遅延 | [COMMANDS.md#cpu](references/COMMANDS.md#cpu負荷) |
| メモリ | OOM、スワップ | [COMMANDS.md#memory](references/COMMANDS.md#メモリ) |
| ディスク | 容量不足、I/O遅延 | [COMMANDS.md#disk](references/COMMANDS.md#ディスク) |
| ネットワーク | 接続失敗、遅延 | [COMMANDS.md#network](references/COMMANDS.md#ネットワーク) |
| プロセス | ハング、ゾンビ | [COMMANDS.md#process](references/COMMANDS.md#プロセス) |
| サービス | 起動失敗 | [COMMANDS.md#service](references/COMMANDS.md#サービス) |
| ログ | エラー調査 | [COMMANDS.md#log](references/COMMANDS.md#ログ) |
| GPU/NVIDIA | nvidia-smi失敗、CUDA/NVEncエラー | [COMMANDS.md#gpu](references/COMMANDS.md#gpunvidia) |
| グラフィック | xrdp黒画面、DRM権限エラー | [COMMANDS.md#graphics](references/COMMANDS.md#グラフィックディスプレイ) |
| Docker/コンテナ | コンテナ内デバイスアクセス不可 | [COMMANDS.md#docker](references/COMMANDS.md#dockerコンテナ) |
| 権限/ACL | デバイスアクセス拒否、グループ不足 | [COMMANDS.md#permission](references/COMMANDS.md#ユーザー権限acl) |

## クイックスタート: 初動診断

問題の切り分けのため、まず以下を実行：

```bash
# 1. システム全体の状態確認
uptime                    # 負荷平均確認
free -h                   # メモリ使用状況
df -h                     # ディスク使用状況

# 2. リソース消費トップ確認
top -bn1 | head -20       # CPU/メモリ上位プロセス

# 3. 最近のエラー確認
dmesg | tail -30          # カーネルメッセージ
journalctl -p err -n 20   # エラーログ（systemd環境）
```

## 診断フローチャート

詳細な診断手順は [FLOWCHART.md](references/FLOWCHART.md) を参照。

### 簡易フロー

```
問題発生
    │
    ├─→ システムが遅い
    │       ├─ uptime で Load Average 確認
    │       ├─ 高い → CPU負荷診断へ
    │       └─ 低い → I/O待ち診断へ
    │
    ├─→ メモリ不足エラー
    │       ├─ free -h で確認
    │       ├─ dmesg | grep -i oom
    │       └─ メモリ診断へ
    │
    ├─→ ディスク関連エラー
    │       ├─ df -h で容量確認
    │       ├─ iostat でI/O確認
    │       └─ ディスク診断へ
    │
    ├─→ ネットワーク接続問題
    │       ├─ ip addr / ping で基本確認
    │       ├─ ss -tlnp でポート確認
    │       └─ ネットワーク診断へ
    │
    ├─→ サービス起動失敗
    │       ├─ systemctl status <service>
    │       ├─ journalctl -u <service>
    │       └─ サービス診断へ
    │
    ├─→ GPU/NVIDIA問題
    │       ├─ nvidia-smi で状態確認
    │       ├─ nvidia-smi -q | grep "Persistence Mode"
    │       └─ GPU診断へ
    │
    ├─→ グラフィック/xrdp問題
    │       ├─ ls -la /dev/dri/ でデバイス確認
    │       ├─ groups | grep video
    │       └─ グラフィック診断へ
    │
    └─→ コンテナ内デバイス問題
            ├─ docker exec <container> nvidia-smi
            ├─ docker inspect でDeviceRequests確認
            └─ コンテナ診断へ
```

## コマンドリファレンス

詳細なコマンドリスト: [COMMANDS.md](references/COMMANDS.md)

### 必須コマンド（覚えておくべき基本）

| 目的 | コマンド | 説明 |
|------|---------|------|
| 負荷確認 | `uptime` | Load Average（1/5/15分） |
| メモリ | `free -h` | メモリ使用状況 |
| ディスク容量 | `df -h` | ファイルシステム使用状況 |
| プロセス | `ps aux` | 全プロセス一覧 |
| リアルタイム監視 | `top` / `htop` | CPU/メモリ監視 |
| ネットワーク | `ip addr` | IPアドレス確認 |
| ポート | `ss -tlnp` | リスニングポート |
| ログ | `journalctl -xe` | 最新ログ（詳細） |

## 診断レベル

### Level 1: 基本診断（初心者向け）

- `uptime`, `free`, `df`, `top`, `ps`
- 問題の大まかな切り分け

### Level 2: 詳細診断（中級者向け）

- `vmstat`, `iostat`, `netstat`/`ss`, `lsof`
- 原因の特定

### Level 3: 高度診断（上級者向け）

- `strace`, `perf`, `tcpdump`, `/proc` 解析
- 根本原因の究明

## 注意事項

- `sudo` が必要なコマンドあり（一部プロセス情報、tcpdump等）
- 本番環境での `strace`, `tcpdump` は慎重に（パフォーマンス影響）
- ログファイルのパスはディストリビューションで異なる場合あり

## 関連ファイル

- [COMMANDS.md](references/COMMANDS.md) - コマンド詳細リファレンス
- [FLOWCHART.md](references/FLOWCHART.md) - 診断フローチャート

## 調査カルテ（共有メモリ）

過去の調査事例は、Claude Code / Codex の両方から参照できる共有メモリで管理する。ローカルの `cartes/` ディレクトリには保存しない。

カルテを参照・作成・更新する場合は `shared-memory` スキルを読み、次の固定分類を使う。

- scope: `project`
- project ID: `linux-diag`
- memory type: `reference`
- entity type / ID: `project` / `linux-diag`
- key: カルテ名の kebab-case（例: `xrdp-drm-permission`）
- summary: 症状、環境、調査過程、根本原因、解決策、学んだことを含むカルテ本文

```bash
# 関連カルテを検索（global メモリを混ぜない）
~/.agents/memory/run-python.sh ~/.agents/memory/memory.py search \
  --query 'xrdp' --project-id linux-diag --scope project
```

診断開始時は症状のキーワードで検索する。診断完了時は、今後も再利用できる確認済みの知見であれば `write-memory` で保存する。同じ key を使うと既存カルテの更新になるため、更新前に必ず `search` で既存内容を確認する。

| カルテ key | タグ |
|-------|------|
| xrdp-drm-permission | xrdp, DRM, video, render, 権限 |
| xrdp-software-rendering | xrdp, GNOME, レンダリング, Mutter, 環境変数 |
| xrdp-service-pidfile-timeout | xrdp, systemd, PIDFile, Type=forking, タイムアウト, ソースビルド |
| docker-gpu-persistence | Docker, GPU, NVIDIA, Persistence Mode |
| docker-gpu-driver-version-mismatch | Docker, GPU, NVIDIA, Driver/library version mismatch, nvidia-persistenced, カーネルモジュール |
