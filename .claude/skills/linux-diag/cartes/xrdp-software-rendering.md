# xrdp接続時の黒画面問題（Mutterレンダリング）

**タグ**: xrdp, GNOME, レンダリング, 黒画面, Mutter, 環境変数
**初回**: 2025-12-15
**更新**: 2026-03-14
**環境**: Ubuntu 24.04, GNOME Shell 46 (Mutter 46.2), NVIDIA GPU, xrdp 0.10.3

---

## 症状

- xrdp認証は成功、Xorgサーバー・GNOME Shell共に起動する
- しかしRDPクライアントには黒画面が表示される
- X11フレームバッファ（xwd）では白画面（ソフトウェアレンダリング時）または黒画面（glamor時）
- GNOME Shellプロセスは正常動作中でD-Bus応答もある

## 発生条件

- `.xsessionrc`や`.xsession`で`MUTTER_DEBUG_FORCE_KMS_MODE=simple`を設定している場合
- Mutterのパッチアップデート（46.2-1ubuntu0.24.04.10以降）でこの変数の挙動が変化

## 調査プロセス

### Step 1: xrdp/Xorgログ確認

```bash
$ journalctl -u xrdp -n 30 --no-pager
[INFO] login was successful - creating session
[INFO] connected to Xorg_pid=XXXX
[INFO] Received memory_allocation_complete command
# → 接続は成立している

$ cat ~/.xorgxrdp.10.log | grep -E "(DRM|DRI|glamor|DRISWRAST)"
rdpPreInit: /dev/dri/renderD128 open failed
GLX: Initialized DRISWRAST GL provider for screen 0
# → ソフトウェアレンダリングにフォールバック
```

### Step 2: GNOME Shell応答確認

```bash
$ DISPLAY=:10 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  dbus-send --session --dest=org.gnome.Shell --type=method_call --print-reply \
  /org/gnome/Shell org.gnome.Shell.Eval string:'Meta.is_wayland_compositor()'
# → boolean false（X11モードで正常応答）
```

→ GNOME Shellは動作しているが画面が描画されていない

### Step 3: Xフレームバッファ直接確認

```bash
$ DISPLAY=:10 xwd -root -out /tmp/screen.xwd
$ ffmpeg -y -i /tmp/screen.xwd -update 1 -frames:v 1 /tmp/screen.png
# → 白画面または黒画面（Mutterの描画がフレームバッファに到達していない）
```

### Step 4: 環境変数の切り分け

```bash
# .xsessionrcを一時的に無効化
$ mv ~/.xsessionrc ~/.xsessionrc.bak
$ mv ~/.xsession ~/.xsession.bak
$ loginctl terminate-session <session-id>
# → RDP再接続で表示される → 環境変数が原因
```

## 根本原因

**`.xsessionrc`の`MUTTER_DEBUG_FORCE_KMS_MODE=simple`がMutterアップデートで挙動変化**

- この環境変数はMutterのデバッグ用で、KMS（Kernel Mode Setting）の挙動を変更する
- Mutter 46.2の初期パッチ（.10以前）では問題なく動作していた
- .12〜.14のアップデートでこの変数の内部処理が変わり、X11フレームバッファへの描画がスキップされるようになった
- 結果、xorgxrdpのダメージイベントキャプチャに何も届かず黒画面に

### 無関係だった要因

| 要因 | 状態 | 結果 |
|------|------|------|
| renderグループ未所属 | 修正済（usermod -aG render） | 黒画面は改善せず |
| DRMAllowListにnvidiaなし | nvidia追加テスト | glamor有効化されたが黒画面 |
| xorgxrdpバージョン不一致 | 0.10.3に再コンパイル | 黒画面は改善せず |
| xrdpサービス異常 | 正常動作 | - |

## 解決策

### `.xsessionrc`の修正

```bash
# 必要な環境変数のみ設定（Mutterデバッグ変数は使わない）
export GNOME_SHELL_SESSION_MODE=ubuntu
export XDG_CURRENT_DESKTOP=ubuntu:GNOME
export XDG_DATA_DIRS=/usr/share/ubuntu:/usr/local/share:/usr/share:/var/lib/snapd/desktop
export XDG_CONFIG_DIRS=/etc/xdg/xdg-ubuntu:/etc/xdg
```

### 削除すべき環境変数

| 環境変数 | 理由 |
|---------|------|
| `MUTTER_DEBUG_FORCE_KMS_MODE=simple` | **黒画面の直接原因**。Mutterアップデートで挙動変化 |
| `LIBGL_ALWAYS_SOFTWARE=1` | 不要。xorgxrdpが自動でレンダリングパスを選択 |
| `GALLIUM_DRIVER=llvmpipe` | 不要。上記と同様 |

### カスタム`.xsession`は不要

`/etc/X11/Xsession`経由の標準フローで`.xsessionrc`を読み込むため、
カスタム`.xsession`スクリプトは不要（むしろD-Bus二重起動等のリスクがある）。

### 適用手順

```bash
# 1. .xsessionrc を上記内容に修正
# 2. .xsession があれば削除またはリネーム
# 3. セッション終了＆再接続
loginctl terminate-session <session-id>
```

## 学んだこと

1. **Mutterの`MUTTER_DEBUG_*`変数はデバッグ用であり、アップデートで予告なく挙動が変わる**。本番環境で恒久的に使用してはいけない
2. xrdp環境では`LIBGL_ALWAYS_SOFTWARE`等の強制も不要。xorgxrdpが適切にフォールバックする
3. 黒画面の切り分けには`xwd -root`でXフレームバッファを直接確認するのが有効
4. `.xsessionrc`（環境変数のみ）と`.xsession`（起動スクリプト）の役割を混同しない

## 関連カルテ

- [xrdp-drm-permission.md](xrdp-drm-permission.md) - DRM権限（videoグループ）の問題
