# NVIDIAドライババージョン不一致でコンテナのGPUマウントが失敗

**タグ**: Docker, GPU, NVIDIA, Driver/library version mismatch, nvidia-persistenced, カーネルモジュール
**日時**: 2026-08-10
**環境**: Ubuntu 24.04, NVIDIA GeForce GTX 1080 Ti, nvidia-driver-580, Docker Compose + nvidia-container-toolkit

---

## 症状

- `docker compose --profile prod up -d`（プロジェクトの `start.sh prod`）実行時にコンテナ起動が失敗
- エラー:

```
Error response from daemon: failed to create task for container: failed to create shim task:
OCI runtime create failed: runc create failed: unable to start container process:
error during container init: failed to fulfil mount request:
open /run/nvidia-persistenced/socket: no such file or directory
```

- 直前まで通常運用できていた（数週間ぶりに prod を再起動しようとして発覚したケース）

## 調査プロセス

### Step 1: nvidia-persistenced サービスの状態確認

```bash
$ systemctl status nvidia-persistenced --no-pager
○ nvidia-persistenced.service - NVIDIA Persistence Daemon
     Active: inactive (dead) since ...（2週間前）
```

→ **サービスが停止しており、`/run/nvidia-persistenced/` ディレクトリ自体が存在しない**（`ls` で No such file or directory）。Docker はこのソケットをコンテナへ bind mount しようとするため、ディレクトリごと無ければマウント要求の時点で失敗する。

### Step 2: ホスト側で nvidia-smi を直接実行

```bash
$ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 580.173
```

→ nvidia-persistenced が落ちているだけでなく、**ホスト側の NVML 自体が初期化できていない**。単なるサービス再起動では直らない可能性が高いと判断。

### Step 3: カーネルにロード中のモジュールとディスク上のパッケージのバージョンを比較

```bash
$ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.159.03  Fri Apr 24 06:16:47 UTC 2026

$ modinfo nvidia | grep version
version:        580.173.02
```

→ **カーネルにロード中のモジュール（580.159.03）とディスク上のパッケージ（580.173.02）が一致していない**。

### Step 4: 最終再起動日時を確認

```bash
$ last reboot | head -3
reboot   system boot  ...  Sat Jun 27 10:26   still running
```

→ 直近で `nvidia-driver-580` パッケージが `580.159.03` → `580.173.02` に apt アップグレードされていたが、その後一度もシステム再起動されていなかった。

## 根本原因

**NVIDIAドライバのカーネルモジュールとユーザー空間ライブラリのバージョン不一致**

1. `apt upgrade` 等で `nvidia-driver-580` パッケージが更新される（ディスク上のモジュールファイル・ユーザー空間ライブラリは新バージョンに置き換わる）
2. カーネルには**旧バージョンのモジュールが既にロード済み**のため、reboot（またはモジュールの明示的な unload/reload）が起きない限り新バージョンに切り替わらない
3. `nvidia-smi` 等のユーザー空間ツールは新バージョンのライブラリを参照するため、カーネルモジュールとの間で `Driver/library version mismatch` が発生し NVML 初期化に失敗
4. NVML に依存する `nvidia-persistenced` も同様に失敗し、サービスが停止 → `/run/nvidia-persistenced/socket` が消滅
5. `docker compose` は nvidia-container-toolkit 経由でこのソケットをコンテナにマウントしようとするが、ファイルごと存在しないため `failed to fulfil mount request` で起動失敗

## 解決策

**システムの再起動**が最も確実（GPU を掴んでいるプロセス（X サーバー等）がある状態でのモジュール unload/reload は失敗しやすく、確実性に劣るため推奨しない）。

```bash
sudo reboot
```

### 再起動後の確認手順

```bash
# 1. カーネルモジュールとライブラリのバージョンが一致しているか
cat /proc/driver/nvidia/version
modinfo nvidia | grep version

# 2. nvidia-smi が version mismatch なく動くか
nvidia-smi

# 3. nvidia-persistenced が起動し、ソケットが存在するか
systemctl status nvidia-persistenced --no-pager
ls -la /run/nvidia-persistenced/

# 4. Persistence Mode が有効か
nvidia-smi -q | grep "Persistence Mode"

# 5. GPUを要求しているサービスの特定（compose.yml で確認）
grep -n -B5 'driver: nvidia' compose.yml
# → devices/capabilities: [gpu] を持つサービス名を確認する
#   （全サービスがGPUを要求しているとは限らない。今回の環境では ollama のみだった）

# 6. prod環境の起動、および該当コンテナ内からのGPU認識確認
./start.sh prod
docker exec <GPUを要求するサービスのコンテナ名> nvidia-smi
```

## 学んだこと

1. **エラーメッセージの発生源と真因が離れていることがある**: Docker のマウントエラーとして表面化するが、真因はホストのドライバ状態。エラーメッセージだけを見て `nvidia-container-toolkit` や compose 設定を疑う前に、まずホスト側で直接 `nvidia-smi` を叩いて NVML 自体が正常か確認するのが近道。
2. **`nvidia-persistenced` の停止は症状であって原因ではないことがある**: 関連カルテ [docker-gpu-persistence.md](docker-gpu-persistence.md) は「Persistence Mode が明示的に無効化されている」ケースだが、今回は「NVML 自体が初期化できずデーモンが道連れで落ちた」ケースであり、対処法が異なる（前者はサービス設定の恒久化、後者はシステム再起動）。両者は `nvidia-smi` のエラー内容（`Persistence Mode: Disabled` か `Driver/library version mismatch` か）で切り分けられる。
3. **`/proc/driver/nvidia/version` と `modinfo nvidia` の比較は version mismatch の一次切り分けに有効**: 前者はカーネルにロード中の実体、後者はディスク上のパッケージ実体。両者がズレていれば「更新後の再起動待ち」が濃厚。
4. **apt でドライバを更新したら reboot するまでは「半分更新された」不安定な状態が続く**: このホストは 2026-06-27 の再起動以降 1ヶ月半以上再起動されておらず、その間にドライバ更新が入ったことで発覚が遅れた。定期的な再起動運用、またはドライバ更新直後の速やかな再起動が望ましい。
