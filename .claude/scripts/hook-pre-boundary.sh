#!/usr/bin/env bash
# PreToolUse: Write/Edit がプロジェクトルート外を対象にしていたらブロック
# 対応ルール: file-operations.md
# 例外: ~/.claude/skills 配下と、このフックが属するリポジトリの .claude/skills 配下への書き込みは許可
# 例外: ~/.claude/projects/*/memory/ 配下への書き込みは許可（auto memory 機能）
# 例外: ${TMPDIR:-/tmp}/claude-<UID>/<スラグ>/<session_id>/ 配下への書き込みは許可（スクラッチパッド）
# 依存コマンド（jq と、-m を使える realpath / grealpath）が使えないときは、理由を stderr に出して
# exit 2 で拒否する。Claude Code は exit 1 や 127 では呼び出しを止めず、素通りになるため
# 判定に値を使うコマンド置換は、どれも終了コードを確かめて失敗なら拒否する（名前を挙げたコマンド以外の失敗も許可に倒さない）

# パスの前方一致と制御文字の除去をバイト単位で決定させる（ロケール非依存にする）
export LC_ALL=C

# PreToolUse の exit 2 はツール呼び出しを止め、stderr が理由として Claude に渡る。
# jq に頼れない状況でも拒否できるよう、JSON ではなく終了コードで伝える
fail_closed() {
  printf '%s\n' "$1" >&2
  exit 2
}

deny() {
  if ! jq -nc --arg reason "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'; then
    fail_closed "$1（拒否の判定を JSON で出力できなかったため、終了コードで拒否しました）"
  fi
  exit 0
}

# stdin は一度だけ読む。file_path と session_id の両方をこのペイロードから取り出す。
# 依存コマンドの確認より先に読み切る（読まずに終了すると、書き込み側がパイプの切断を受けうるため）
payload=$(cat) || fail_closed "標準入力を読み取れないため、書き込みを拒否しました"

jq -n 'empty' >/dev/null 2>&1 \
  || fail_closed "jq を実行できないため、書き込みを拒否しました。jq をインストールし、Claude Code を起動する環境の PATH に入れてください"

# realpath -m: 対象が存在しなくても正規化する。macOS 標準の realpath はコマンドとしては存在するが
# -m を持たないため、command -v ではなく -m 付きで実際に動くかで選ぶ（grealpath は Homebrew の coreutils）
if realpath -m / >/dev/null 2>&1; then
  realpath_cmd=realpath
elif grealpath -m / >/dev/null 2>&1; then
  realpath_cmd=grealpath
else
  fail_closed "realpath -m を実行できないため、書き込みを拒否しました。GNU coreutils をインストールしてください（macOS は brew install coreutils で grealpath が入ります）"
fi

if ! f=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null); then
  deny "入力JSONの解析に失敗しました"
fi
[ -z "$f" ] && exit 0

# 拒否理由の文言にだけ使う。tr が失敗しても理由が空になるだけで、拒否は維持される
f_safe=$(printf '%s' "$f" | tr -d '\000-\037')
unresolved="書き込み先または許可範囲のパスを解決できなかったため、書き込みをブロック: ${f_safe}"

# 既定の realpath は存在しないパスで失敗して空文字を返し、前方一致の判定が "/*"（全許可）に化ける。
# -s は付けない（シンボリックリンクで境界を抜けられる）
real_f=$("$realpath_cmd" -m "$f") || deny "$unresolved"
# プロジェクトルートはフックを起動したプロセスの cwd ではなく CLAUDE_PROJECT_DIR を基準にする
real_project=$("$realpath_cmd" -m "${CLAUDE_PROJECT_DIR:-$PWD}") || deny "$unresolved"
real_skills=$("$realpath_cmd" -m "$HOME/.claude/skills") || deny "$unresolved"
# skill はリポジトリから skill 単位でリンクされ、書き込み先はリンクを辿ってリポジトリ側に解決される。
# そのため、フックの実体が属するリポジトリの skills も許可する
# 実体パスの解決に失敗したまま親ディレクトリを取ると、cwd 相対や "/skills" を許可してしまう。
# そのため実体パスを先に求め、失敗や空を拒否してから親を取る
hook_path=$("$realpath_cmd" -m "${BASH_SOURCE[0]}") || deny "$unresolved"
# hook_path は realpath の出力で必ず絶対パスのため、dirname の代わりにパラメータ展開で親を取る（外部コマンドへの依存を減らす）
hook_dir=${hook_path%/*}
real_repo_skills=$("$realpath_cmd" -m "$hook_dir/../skills") || deny "$unresolved"
real_home=$("$realpath_cmd" -m "$HOME") || deny "$unresolved"
scratch_base=$("$realpath_cmd" -m "${TMPDIR:-/tmp}/claude-$EUID") || deny "$unresolved"

# 成功の終了コードで空を返された場合の守り。空の範囲は "$var"/* が "/*"（全許可）と同じ意味になるため、許可範囲として扱わない
for resolved in "$real_f" "$real_project" "$real_skills" "$hook_path" "$real_repo_skills" "$real_home" "$scratch_base"; do
  [ -n "$resolved" ] || deny "$unresolved"
done

case "$real_f" in
  "$real_project"/*|"$real_project") exit 0 ;;
  "$real_skills"/*|"$real_skills") exit 0 ;;
  "$real_repo_skills"/*|"$real_repo_skills") exit 0 ;;
  # <スラグ>/<session_id>/<1 つ以上の要素> の深さを要求する（スラグ自体は照合しない）
  "$scratch_base"/*/*/*)
    session_id=$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null) \
      || deny "入力JSONから session_id を取り出せなかったため、書き込みをブロック: ${f_safe}"
    # session_id が取れない場合はスクラッチパッドを許可しない（fail-closed）
    if [ -n "$session_id" ]; then
      rest=${real_f#"$scratch_base"/}
      after_slug=${rest#*/}
      # [ = ] のリテラル比較で突き合わせる（glob・正規表現として解釈させない）
      [ "${after_slug%%/*}" = "$session_id" ] && exit 0
    fi
    ;;
esac

# $real_home はクォートによりリテラル扱い（メタ文字を含む環境でも安全）
if [[ "$real_f" =~ ^"$real_home"/\.claude/projects/[^/]+/memory/ ]]; then
  exit 0
fi

deny "プロジェクト外への書き込みをブロック: ${f_safe}"
