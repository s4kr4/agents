# xrdp.service が起動タイムアウトで failed（PIDファイル不整合）

**タグ**: xrdp, systemd, PIDFile, Type=forking, タイムアウト, ソースビルド, 0.10.x
**初回**: 2026-05-29
**更新**: 2026-05-29
**環境**: Linux 6.17（Ubuntu系）, xrdp 0.10.3（ソースビルド `/usr/local/sbin/xrdp`）, systemd

---

## 症状

- RDPクライアントから接続できない（接続要求がタイムアウト／拒否）
- `systemctl status xrdp` が `failed (Result: timeout)`
- `xrdp-sesman` は `active (running)` で動いている（一見正常に見える）
- ポート 3389 が `ss -tlnp` でリッスンされていない

## 調査プロセス

### Step 1: サービス状態確認

```bash
$ systemctl status xrdp xrdp-sesman --no-pager
× xrdp.service - xrdp daemon
   Active: failed (Result: timeout) since ...
● xrdp-sesman.service - xrdp session manager
   Active: active (running)
```

→ xrdp 本体だけが落ちている。sesman は生きているのが特徴。

### Step 2: ポートリッスン確認

```bash
$ ss -tlnp | grep 3389
（出力なし）
```

→ port 3389 は誰もリッスンしていない。

### Step 3: ログで決定的なエラーを発見

```bash
$ journalctl -u xrdp -n 50 --no-pager
xrdp[1608]: [INFO ] listening to port 3389 on 0.0.0.0
xrdp[1608]: [INFO ] starting xrdp with pid 1608
systemd[1]: xrdp.service: Failed to parse PID from file /run/xrdp.pid: Invalid argument
systemd[1]: xrdp.service: start operation timed out. Terminating.
systemd[1]: xrdp.service: Failed with result 'timeout'.
```

→ xrdp バイナリ自体は port 3389 までリッスン成功している。しかし systemd が **PIDファイル `/run/xrdp.pid` を解析できず**、起動完了と判定できないまま 90秒タイムアウトで kill されている。

### Step 4: PIDファイルの実在確認

```bash
$ ls -la /run/xrdp.pid /var/run/xrdp.pid
ls: /run/xrdp.pid: No such file or directory
ls: /var/run/xrdp.pid: No such file or directory
```

→ PIDファイル自体が作られていない。

### Step 5: unit ファイル確認

```bash
$ cat /etc/systemd/system/xrdp.service
[Service]
Type=forking
ExecStart=/usr/local/sbin/xrdp
PIDFile=/var/run/xrdp.pid
```

→ `Type=forking` + `PIDFile=` の組み合わせ。systemd は「PIDファイル出現＝起動完了」と判定する仕様だが、xrdp 0.10.3 はそれを書いていない。

### Step 6: xrdp バイナリの起動オプション確認

```bash
$ /usr/local/sbin/xrdp --help
   -n, --nodaemon    don't fork into background
   ...
  Configure options:
      --with-systemdsystemunitdir=/usr/lib/systemd/system
```

→ `--nodaemon` オプションが利用可能。`Type=simple` で前面実行できる。

## 根本原因

- xrdp 0.10.x（ソースビルド版）は **PIDファイルを期待どおりの形式で書き出さない**
- 一方、`/etc/systemd/system/xrdp.service` は古い `Type=forking` + `PIDFile=` パターンで書かれている
- systemd は PIDファイル出現を待ち続け、デフォルトの起動タイムアウト（90秒）で xrdp プロセスを kill
- 結果、port 3389 を listen するプロセスがいなくなり接続不可

## 解決策

`Type=simple` + `--nodaemon` に切り替える。

```bash
# 1. バックアップ
sudo cp /etc/systemd/system/xrdp.service /etc/systemd/system/xrdp.service.bak

# 2. unit ファイルを以下の内容に書き換え
sudoedit /etc/systemd/system/xrdp.service

# 3. 再読み込み & 再起動
sudo systemctl daemon-reload
sudo systemctl restart xrdp

# 4. 確認
systemctl status xrdp --no-pager
ss -tlnp | grep 3389
```

**書き換え後の unit ファイル:**

```ini
[Unit]
Description=xrdp daemon
Requires=xrdp-sesman.service
After=network.target xrdp-sesman.service

[Service]
Type=simple
ExecStart=/usr/local/sbin/xrdp --nodaemon
ExecReload=/bin/kill -s HUP $MAINPID

[Install]
WantedBy=multi-user.target
```

**変更ポイント:**

- `Type=simple`: xrdp が fork せず前面実行 → systemd が直接 PID を保持する
- `--nodaemon`: xrdp バイナリに daemon 化しないよう指示
- `PIDFile=` 削除: 不要になった
- `Requires=xrdp-sesman.service` + `After=xrdp-sesman.service`: sesman を先に起動することを保証

## なぜ xrdp-sesman 側は同じ unit パターンで動くのか

- xrdp-sesman 側も `Type=forking` + `PIDFile=/var/run/xrdp-sesman.pid` のまま動作している
- sesman バイナリは fork + PID 書き出しが期待通りに動いている、もしくは systemd が fork トラッキングのフォールバックを使えている可能性
- **動いているうちは触らない**のが安全（同時に複数箇所を変えるとロールバック判断が難しくなる）

## 注意: 切り分けポイント

xrdp 接続不可には複数のレイヤがある。順番に切り分ける：

| レイヤ | 症状 | 確認コマンド | 該当カルテ |
|--------|------|------------|----------|
| サービス起動 | port 3389 未リッスン、`failed` | `systemctl status xrdp` / `ss -tlnp \| grep 3389` | このカルテ |
| セッション（DRM） | 認証成功するが黒画面 | `tail ~/.xorgxrdp.*.log` | xrdp-drm-permission.md |
| セッション（レンダリング） | GNOMEが起動しない、ロゴで停止 | `~/.xsession-errors` | xrdp-software-rendering.md |

## 学んだこと

1. xrdp 0.10.x ソースビルド版は、古い `Type=forking` + `PIDFile=` パターンと相性が悪い
2. xrdp バイナリは port 3389 まで listen 成功しても、systemd 側の起動判定で kill されることがある（ログをよく読むこと）
3. `Failed to parse PID from file ... Invalid argument` のエラーが出たら、unit の `Type` 戦略を疑う
4. `Type=simple` + `--nodaemon` は近年の daemon プロセスの標準的な systemd 統合方式
5. xrdp-sesman と xrdp で挙動差がある（sesman は動いているのに xrdp は落ちる）パターンは、サービスごとに別問題の可能性。同時にいじらず一つずつ修正する

## 関連カルテ

- [xrdp-drm-permission.md](xrdp-drm-permission.md) - 接続後の黒画面（DRM権限の問題）
- [xrdp-software-rendering.md](xrdp-software-rendering.md) - GNOME/Mutterのレンダリング問題
