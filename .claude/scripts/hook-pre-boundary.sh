#!/usr/bin/env bash
# PreToolUse: Write/Edit がプロジェクトルート外を対象にしていたらブロック
# 対応ルール: file-operations.md
# 例外: ~/.claude/skills 配下と、このフックが属するリポジトリの .claude/skills 配下への書き込みは許可
# 例外: ~/.claude/projects/*/memory/ 配下への書き込みは許可（auto memory 機能）
# 例外: ${TMPDIR:-/tmp}/claude-<UID>/<スラグ>/<session_id>/ 配下への書き込みは許可（スクラッチパッド）
# 例外: MSYS/Cygwin に限り ${TMPDIR:-/tmp}/claude/<スラグ>/<session_id>/ 配下も許可
#       （Windows の一時ディレクトリはユーザーごとに分かれるため Claude Code は UID 成分の
#         無い基点を使う。共有される POSIX の /tmp では UID 成分が基点の先置きを防ぐので通さない）
# 想定する file_path の表記は POSIX 絶対パスと、MSYS/Cygwin に限りドライブ絶対パスだけ。
# それ以外（先頭に区切り / か円記号が 2 つ並ぶ UNC などの表記・ドライブ相対・相対パス）は、どの例外よりも先に拒否する
# 基点（CLAUDE_PROJECT_DIR・HOME など）が正規化してファイルシステムのルート（/、MSYS/Cygwin では
# ドライブのルート /<英字> も）になる場合、その基点は許可に使わない
#
# 契約: 判定できない入力・正規化できない入力は JSON で deny する（fail-closed）。
# 依存コマンド（jq と、-m を使える realpath / grealpath、MSYS/Cygwin では cygpath）が使えないときは、
# 理由を stderr に出して exit 2 で拒否する。Claude Code は exit 1 や 127 では呼び出しを止めず、素通りになるため
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

# プラットフォームの判定はツールの有無ではなく uname -s で行う。ツールの有無で分岐させると、
# cygpath を失った MSYS が「POSIX なので変換不要」と自称して素通しになる
uname_s=$(uname -s 2>/dev/null) || uname_s=""
case "$uname_s" in
  MINGW*|MSYS*|CYGWIN*) is_msys=yes ;;
  *) is_msys=no ;;
esac

# MSYS で cygpath が無ければ表記を畳めず、境界の前方一致が意味を失う。素通しは全許可と同義
if [ "$is_msys" = yes ] && ! command -v cygpath >/dev/null 2>&1; then
  fail_closed "cygpath を実行できないため、書き込みを拒否しました。MSYS/Cygwin では cygpath を PATH に入れてください"
fi

if ! f=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null); then
  deny "入力JSONの解析に失敗しました"
fi
[ -z "$f" ] && exit 0

# 拒否理由の文言にだけ使う。tr が失敗しても理由が空になるだけで、拒否は維持される
f_safe=$(printf '%s' "$f" | tr -d '\000-\037')
unsupported="対応していないパス表記をブロック: ${f_safe}"
unresolved="書き込み先または許可範囲のパスを解決できなかったため、書き込みをブロック: ${f_safe}"

# 想定する表記は POSIX 絶対パスと Windows のドライブ絶対パスだけ。ドライブ相対（C:docs）・
# UNC やデバイス・長いパスの接頭辞（//server, \\server, /\?\C:\...）・相対パスは、正規化の過程で
# ドライブのカレント位置やマウント表を経由して別の場所へ読み替えられる。読み替え先を根拠に許可しない（fail-closed）。
# cygpath は / と円記号を同じ区切りとして扱うため、先頭 2 文字は組み合わせを問わず区切りの集合で判定する
# （//・\\ だけを挙げると /\ や \/ が /* の分岐を通り、cygpath -u で // へ読み替えられる）。
# ドライブ絶対パスが絶対パスになるのは MSYS/Cygwin だけで、POSIX では cwd 相対として解決されるため通さない
case "$f" in
  [/\\][/\\]*) deny "$unsupported" ;;
  /*) ;;
  [A-Za-z]:[/\\]*) [ "$is_msys" = yes ] || deny "$unsupported" ;;
  *) deny "$unsupported" ;;
esac

# 1 段ぶんの変換をまとめて適用する。入力 1 件につき 1 行が返ることを行数で確かめ、
# 改行を含むパスなどで対応が崩れた場合は正規化失敗として扱う。
# 失敗の終了コードと一緒に出力された値は使わない。
# 先頭で stage_out を空にするのは、失敗時に前回の正規化結果が残り、呼び出し側が失敗の確認を
# 漏らしたときに別のパスの正規形として使われるのを防ぐため（空なら後段の判定はどれも許可に倒れない）
stage_out=()
stage() {
  stage_out=()
  local cmd=$1 opt=$2
  shift 2
  local output
  output=$("$cmd" "$opt" -- "$@" 2>/dev/null) || return 1
  mapfile -t stage_out <<<"$output"
  [ "${#stage_out[@]}" -eq "$#" ]
}

# 判定に使う基点と file_path を同じ正規形へ揃える。file_path 側だけを変換すると
# CLAUDE_PROJECT_DIR が Windows 表記で渡る経路で前方一致が外れる。
#
# MSYS では cygpath -u → realpath -m → cygpath -w → cygpath -u → realpath -m の順に合成する。
# - realpath を後段に回さないと Windows 表記が realpath を素通しし、シンボリックリンクの
#   解決（-s を付けない理由）が効かないまま判定される
# - realpath -m を cygpath -w より先に置くのは、cygpath -w が `..` を含む非実在パスで
#   失敗するため。先に `..` を畳んでから Windows 表記へ渡す
# - cygpath -w を挟むのは、/tmp が Windows の一時ディレクトリのマウント別名で、
#   同じ場所を指す POSIX ドライブ表記（/c/Users/.../Temp/...）を cygpath -u が書き換えず、
#   2 表記が一致しないため。Windows 表記を経由して初めて同じ別名へ落ちる
# 引数はまとめて渡す。このフックは Write/Edit のたびに走るので、パスの件数に比例して
# 外部コマンドを起動すると体感に響く（1 回の呼び出しにつき cygpath 3 回・realpath 2 回に固定する）
normalize_all() {
  local -a acc=("$@")
  if [ "$is_msys" = yes ]; then
    stage cygpath -u "${acc[@]}" || return 1
    acc=("${stage_out[@]}")
    stage "$realpath_cmd" -m "${acc[@]}" || return 1
    acc=("${stage_out[@]}")
    stage cygpath -w "${acc[@]}" || return 1
    acc=("${stage_out[@]}")
    stage cygpath -u "${acc[@]}" || return 1
    acc=("${stage_out[@]}")
  fi
  # realpath -m: 対象が存在しなくても正規化する（既定の realpath は存在しないパスで失敗して空を返す）。
  # -s は付けない（シンボリックリンクで境界を抜けられる）。cygpath は表記を移すだけでリンクを辿らないので代用にならない
  stage "$realpath_cmd" -m "${acc[@]}"
}

norm_names=()
norm_values=()
add_input() {
  norm_names+=("$1")
  norm_values+=("$2")
}

# 名前は real_<名前> の変数へそのまま展開されるので、f は real_f になる
add_input f "$f"
# プロジェクトルートはフックを起動したプロセスの cwd ではなく CLAUDE_PROJECT_DIR を基準にする
project_raw=${CLAUDE_PROJECT_DIR:-$PWD}
[ -n "$project_raw" ] && add_input project "$project_raw"
# HOME が空のまま "$HOME/.claude/skills" を組み立てると基点が /.claude/skills に化け、
# 隔離された環境やサービス実行で無関係なツリーを開いてしまう
if [ -n "$HOME" ]; then
  add_input home "$HOME"
  add_input skills "$HOME/.claude/skills"
fi
# skill はリポジトリから skill 単位でリンクされ、書き込み先はリンクを辿ってリポジトリ側に解決される。
# そのため、フックの実体が属するリポジトリの skills も許可する。親はリンクを辿った実体から取る必要が
# あるので、ここではフック自身のパスを正規化し、skills は後で求める。
# BASH_SOURCE は MSYS で円記号区切りの Windows 表記になりうるが、正規化が表記をそろえる
add_input hook "${BASH_SOURCE[0]}"
tmp_base=${TMPDIR:-/tmp}
add_input scratch "$tmp_base/claude-$EUID"
[ "$is_msys" = yes ] && add_input scratch_shared_tmp "$tmp_base/claude"

normalize_all "${norm_values[@]}" || deny "$unresolved"

real_f=""
real_project=""
real_home=""
real_skills=""
real_hook=""
real_scratch=""
real_scratch_shared_tmp=""
for index in "${!norm_names[@]}"; do
  printf -v "real_${norm_names[index]}" '%s' "${stage_out[index]}"
done

[ -n "$real_f" ] || deny "$unresolved"

# 正規化の出力は絶対パスのため、dirname の代わりにパラメータ展開で親を取る（外部コマンドへの依存を減らす）。
# 実体パスが空や "/x" のまま親を取ると、cwd 相対や "/skills" を許可してしまうため、その範囲は使わない
real_repo_skills=""
case "$real_hook" in
  /*/*)
    normalize_all "${real_hook%/*}/../skills" || deny "$unresolved"
    real_repo_skills=${stage_out[0]}
    ;;
esac

# 正規形が得られなかった基点と、ファイルシステムのルートは基点として使わない。
# 空文字のまま前方一致に使うと "/*"（全許可）に、ルートのままだとファイルシステム全体を開く基点になる。
# MSYS ではドライブのルート（/c）も POSIX の "/" と同じく 1 つのファイルシステム全体を指す。
# POSIX では /c は普通のディレクトリなので捨てない。Cygwin では /c も捨てるが、制限を強める側に倒れるだけ
# （Cygwin のドライブのルート /cygdrive/<英字> は想定しない）。
# UNC 共有のルート（//host/share）を基点にする場合も想定しない。到達には共有のルートでの起動と
# UNC を指すリンクの経由の両方が要り、起きても許可されるのはプロジェクトの内側だけ
# （// で始まる file_path は表記の判定で先に拒否される）
is_filesystem_root() {
  case "$1" in
    /) return 0 ;;
    /[A-Za-z]) [ "$is_msys" = yes ] ;;
    *) return 1 ;;
  esac
}
for name in real_project real_home real_skills real_repo_skills real_scratch real_scratch_shared_tmp; do
  if [ -z "${!name}" ] || is_filesystem_root "${!name}"; then
    printf -v "$name" '%s' ""
  fi
done

# 範囲は名前の切れ目で終わる（基点そのものか、その配下）。skills-archive のような兄弟は含めない
within() {
  [ -n "$1" ] || return 1
  case "$real_f" in
    "$1"|"$1"/*) return 0 ;;
  esac
  return 1
}

within "$real_project" && exit 0
within "$real_skills" && exit 0
within "$real_repo_skills" && exit 0

session_id=""
session_id_read=no
read_session_id() {
  [ "$session_id_read" = yes ] && return 0
  session_id_read=yes
  # 失敗の終了コードと一緒に出力された値は信じない
  session_id=$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null) || session_id=""
  return 0
}

scratchpad_allows() {
  local base=$1 rest after_slug
  [ -n "$base" ] || return 1
  # <スラグ>/<session_id>/<1 つ以上の要素> の深さを要求する（スラグ自体は照合しない）
  case "$real_f" in
    "$base"/*/*/*) ;;
    *) return 1 ;;
  esac
  read_session_id
  # session_id が取れない場合はスクラッチパッドを許可しない（fail-closed）
  [ -n "$session_id" ] || return 1
  rest=${real_f#"$base"/}
  after_slug=${rest#*/}
  # [ = ] のリテラル比較で突き合わせる（glob・正規表現として解釈させない）
  [ "${after_slug%%/*}" = "$session_id" ]
}

if scratchpad_allows "$real_scratch" || scratchpad_allows "$real_scratch_shared_tmp"; then
  exit 0
fi

# $real_home はクォートによりリテラル扱い（メタ文字を含む環境でも安全）
if [ -n "$real_home" ] && [[ "$real_f" =~ ^"$real_home"/\.claude/projects/[^/]+/memory/ ]]; then
  exit 0
fi

deny "プロジェクト外への書き込みをブロック: ${f_safe}"
