---
name: pnpm-hardening
description: pnpm の基本設定、モノレポ運用、依存 build script 制御、minimumReleaseAge などのサプライチェーン防御設定を設計・更新するときに使う。package.json、pnpm-workspace.yaml、.npmrc、lockfile を確認し、pnpm のバージョン制約を踏まえて安全な設定へ揃える。
---

# pnpm Hardening

pnpm の設定を追加・更新するときに使う。特に以下の依頼で使う。

- pnpm の基本設定を見直したい
- モノレポ向けに pnpm を整えたい
- ゼロデイ対策やサプライチェーン防御を入れたい
- `minimumReleaseAge`、`allowBuilds`、`trustPolicy` などを導入したい
- `latest` 指定や緩い package manager 運用をやめたい

## 先に見るファイル

- ルート `package.json`
- ワークスペース配下の `package.json`
- `pnpm-workspace.yaml`
- 既存の `.npmrc`
- `pnpm-lock.yaml`
- セットアップスクリプトや CI 設定

## 進め方

1. 現在の pnpm バージョンを確認する。
2. どの設定をどこに置くべきか決める。
3. 既存運用を壊さない範囲でセキュリティ設定を追加する。
4. install script を使う依存を棚卸しして、許可リストを明示する。
5. `pnpm install`、`pnpm run`、CI、セットアップスクリプトへの影響を確認する。

## 置き場所の原則

- `pnpm-workspace.yaml`
  ワークスペース全体に効かせたい pnpm 設定を置く。モノレポ方針と防御設定の第一候補。
- ルート `package.json`
  `packageManager`、ルート scripts、ワークスペース全体の依存管理方針を置く。
- 各 package の `package.json`
  `packageManager` の整合や、その package 固有の scripts を管理する。
- `.npmrc`
  レジストリ、認証、CLI の補助設定向け。ワークスペースの運用方針そのものは `pnpm-workspace.yaml` を優先する。

## 推奨ベースライン

pnpm 10 系では、まず以下を検討する。

- `packageManager` を固定する
- `packageManagerStrictVersion: true`
- `verifyDepsBeforeRun: error`
- `minimumReleaseAge`
- `trustPolicy: no-downgrade`
- `blockExoticSubdeps: true`
- `strictDepBuilds: true`
- `allowBuilds`

バージョンの `latest` 指定は再現性と審査性が落ちるので、基本は固定版か少なくとも意図のある range に変える。

## モノレポでの判断基準

- 全 package で `packageManager` を揃える。
- build script の許可はルート方針で一元管理する。
- セットアップスクリプトから `pnpm install` を分離したほうがよい場合がある。
  `verifyDepsBeforeRun` を有効にすると、未 install 状態の `pnpm run ...` は失敗する。
- `pnpm install` 前提の手順は README や `setup.sh` に明示する。

## build script 制御

pnpm 10.26 以降では `allowBuilds` を優先する。古い `onlyBuiltDependencies` より意図が明確で、新規依存の混入にも強い。

方針:

- 必要なネイティブ依存だけを `true` で許可する
- 何となく広く許可しない
- `dangerouslyAllowAllBuilds` は使わない

よく許可候補になりやすいもの:

- `sharp`
- `better-sqlite3`
- `esbuild`

ただし実際に必要かは lockfile と install ログで確認する。

## ゼロデイ・サプライチェーン防御

- `minimumReleaseAge`
  公開直後の版を一定時間入れない。まずは 1440 分を基準に検討する。
- `trustPolicy: no-downgrade`
  既存より trust evidence が弱い公開物への切り替えを拒否する。
- `trustPolicyIgnoreAfter`
  古い公開物まで厳密に縛ると運用コストが高いときに使う。
- `blockExoticSubdeps: true`
  推移依存経由の git / tarball 流入を抑える。

## バージョン制約

追加する設定が、今の pnpm で解釈できるかを先に確認する。特に以下は version gate がある。

- `minimumReleaseAge`: pnpm >= 10.16
- `trustPolicy`: pnpm >= 10.21
- `trustPolicyIgnoreAfter`: pnpm >= 10.27
- `blockExoticSubdeps`: pnpm >= 10.26
- `allowBuilds`: pnpm >= 10.26

足りない場合は、先に `packageManager` を更新する。

## 変更時のチェック

- `latest` 指定を残していないか
- `packageManager` が全 package で揃っているか
- install script 許可が最小権限になっているか
- `pnpm run setup` や CI が `verifyDepsBeforeRun` で壊れないか
- lockfile の意図しない更新が混ざっていないか

## 出力の仕方

変更提案や実装時は、以下を短く説明する。

- 何をどのファイルに置いたか
- その設定が何を防ぐか
- 互換性上の前提 pnpm バージョン
- 実行フロー変更の有無

## よくある修正パターン

### 1. `latest` を使わない

- `package.json` の `latest` を lockfile か明示版へ固定する
- 再 install で差分を確認する

### 2. `onlyBuiltDependencies` から `allowBuilds` へ移す

- 旧設定を消す
- `pnpm-workspace.yaml` に `allowBuilds` を追加する
- 必要依存だけ `true` にする

### 3. `verifyDepsBeforeRun` で setup が壊れる

- `pnpm install` を `setup.sh` の外に出す
- セットアップ手順に `pnpm install` 前提を明記する

### 4. ゼロデイ対策を最小構成で入れる

- `minimumReleaseAge`
- `blockExoticSubdeps`
- `packageManagerStrictVersion`

必要なときだけ `trustPolicy` を足す。
