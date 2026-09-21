#!/usr/bin/env bash
# ステージされた .claude / .codex のスキル・エージェントが揃っているかを検査する。
#
# 契約:
#   既定                    : 検査のみ。ファイルの書き込みも git add も一切行わない。
#                             不整合があれば解消手順を表示して exit 1 する。
#   --auto-sync             : 片側だけがステージされている場合に同期スクリプトを
#                             起動し、生成されたカウンターパートを git add する。
#                             このオプションを渡したときだけ書き込みが発生する。
#   SKIP_SKILL_SYNC_CHECK=1 : 引数の有無を問わず何もせず exit 0 する。
#
# 名前が check- で始まる以上、既定で書き込まない性質は崩さないこと。
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/check-skill-sync.sh [--auto-sync]

Options:
  --auto-sync  Sync the counterpart files and stage them instead of only
               reporting the inconsistencies.
  -h, --help   Show this help.

Environment:
  SKIP_SKILL_SYNC_CHECK=1  Skip the check entirely and exit 0.
EOF
}

# bypass は「何があっても素通しする」契約なので、引数解析より前に効かせる。
if [[ "${SKIP_SKILL_SYNC_CHECK:-}" == "1" ]]; then
    exit 0
fi

auto_sync=0

# 引数は副作用を起こす前に全部読み切る。途中で同期を始めると、後続の不明な
# 引数で拒否する前に書き込みが発生してしまう。
while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto-sync)
            auto_sync=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
sync_script="$repo_root/scripts/sync-claude-codex-skills.sh"

# 不変条件: ここで値を返すパス族は、collect_state のフラグ設定 case の部分集合で
# あり、counterparts_match の case が返されるペアの形をすべて覆うこと。比較できない
# ペアは同期済みとして素通りさせず issue にする。
# 新しいパス族を足すときは両方を必ず併せて更新する。
# 崩したときに何が起きるかは sync_ran の宣言部を参照。
map_counterpart() {
    local path="$1"
    local rel skill rest

    case "$path" in
        .claude/skills/_tracker/*)
            return 1
            ;;
        .claude/skills/*)
            rel="${path#.claude/skills/}"
            skill="${rel%%/*}"
            rest="${rel#"$skill"/}"

            if [[ "$rel" == "$skill" ]]; then
                return 1
            fi

            printf '.codex/skills/%s/%s\n' "$skill" "$rest"
            ;;
        .claude/agents/*.md)
            skill="${path##*/}"
            skill="${skill%.md}"
            printf '.codex/skills/%s/SKILL.md\n' "$skill"
            ;;
        .codex/skills/*)
            rel="${path#.codex/skills/}"
            skill="${rel%%/*}"
            rest="${rel#"$skill"/}"

            if [[ "$skill" == ".system" || "$rel" == "$skill" ]]; then
                return 1
            fi

            if [[ -e ".claude/skills/$skill" ]]; then
                printf '.claude/skills/%s/%s\n' "$skill" "$rest"
            elif [[ -e ".claude/agents/$skill.md" ]]; then
                printf '.claude/agents/%s.md\n' "$skill"
            else
                printf '.claude/skills/%s/%s\n' "$skill" "$rest"
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

compare_tmp=""
index_entries=""
stage_mode=""
stage_oid=""

# This function is invoked indirectly by the EXIT trap installed below.
# shellcheck disable=SC2317
discard_compare_tmp() {
    [[ -n "$compare_tmp" && -d "$compare_tmp" ]] || return 0

    # 比較用の一時領域にはリポジトリの内容が複製される。pre-commit から毎コミット
    # 走るので、残すと一時領域に複製が単調に積み上がる。実削除で回収する。
    #
    # set -e 下では EXIT トラップ内で最後に実行したコマンドの終了ステータスが
    # スクリプトの終了コードを書き換える。後始末の失敗で「整合なので exit 0」が
    # exit 1 に化けないよう、ここで明示的に吸収する。
    rm -rf -- "$compare_tmp" || true
}

ensure_compare_tmp() {
    if [[ -n "$compare_tmp" ]]; then
        return 0
    fi

    compare_tmp="$(mktemp -d)" || return 1
    trap discard_compare_tmp EXIT
}

read_index_entries() {
    local path="$1"

    if ! index_entries="$(git ls-files --stage -- "$path")"; then
        return 2
    fi
    [[ -n "$index_entries" ]]
}

read_stage_entry() {
    local path="$1"
    local stage_path stage entry_path

    read_index_entries "$path" || return 1
    [[ "$index_entries" != *$'\n'* ]] || return 1

    IFS=' ' read -r stage_mode stage_oid stage_path <<< "$index_entries"
    [[ -n "$stage_mode" && -n "$stage_oid" && "$stage_path" == *$'\t'* ]] || return 1

    stage="${stage_path%%$'\t'*}"
    entry_path="${stage_path#*$'\t'}"
    [[ "$stage" == "0" && "$entry_path" == "$path" ]]
}

is_regular_mode() {
    [[ "$1" == "100644" || "$1" == "100755" ]]
}

materialize_stage_pair() {
    local path="$1" oid="$2" counterpart="$3" counterpart_oid="$4"
    local src_file="$compare_tmp/index/$path"
    local dst_file="$compare_tmp/index/$counterpart"

    mkdir -p -- "${src_file%/*}" "${dst_file%/*}" || return 1
    git cat-file blob "$oid" > "$src_file" || return 1
    git cat-file blob "$counterpart_oid" > "$dst_file" || return 1
}

is_text_file() {
    local path="$1"
    local numstat rc

    if numstat="$(git diff --no-index --numstat -- /dev/null "$path")"; then
        rc=0
    else
        rc=$?
    fi
    [[ "$rc" -eq 0 || "$rc" -eq 1 ]] || return 1
    [[ "$numstat" != $'-\t-'* ]]
}

ensure_transform_functions() {
    if ! declare -F to_codex_agent_skill >/dev/null \
        || ! declare -F to_claude_agent >/dev/null; then
        # The concrete source path supports `shellcheck -x`; standalone checks do not follow it.
        # shellcheck source=scripts/sync-claude-codex-skills.sh
        # shellcheck disable=SC1091
        . "$sync_script" || return 1
    fi

    declare -F to_codex_agent_skill >/dev/null \
        && declare -F to_claude_agent >/dev/null
}

counterparts_match() {
    local path="$1" counterpart="$2"
    local path_mode path_oid counterpart_mode counterpart_oid
    local src_file dst_file converted_oid
    local claude_path codex_path claude_oid codex_oid
    local claude_file codex_file forward_file backward_file

    read_stage_entry "$path" || return 1
    path_mode="$stage_mode"
    path_oid="$stage_oid"
    read_stage_entry "$counterpart" || return 1
    counterpart_mode="$stage_mode"
    counterpart_oid="$stage_oid"

    is_regular_mode "$path_mode" && is_regular_mode "$counterpart_mode" || return 1
    ensure_compare_tmp || return 1
    materialize_stage_pair "$path" "$path_oid" "$counterpart" "$counterpart_oid" || return 1

    src_file="$compare_tmp/index/$path"
    dst_file="$compare_tmp/index/$counterpart"
    is_text_file "$src_file" && is_text_file "$dst_file" || return 1

    case "$path:$counterpart" in
        .claude/skills/*:.codex/skills/*|.codex/skills/*:.claude/skills/*)
            [[ "$path_mode" == "$counterpart_mode" && "$path_oid" == "$counterpart_oid" ]]
            ;;
        .claude/agents/*.md:.codex/skills/*/SKILL.md|.codex/skills/*/SKILL.md:.claude/agents/*.md)
            ensure_transform_functions || return 1

            # 判定の基準は「どちらがステージされたか」ではなくパス族だけで決める。
            # ステージの向きを基準にすると、ステージされた側の表記が常に正しい前提に
            # なり、片側ステージのときだけ不正なミラーが素通りする。
            if [[ "$path" == .claude/agents/* ]]; then
                claude_path="$path"
                claude_oid="$path_oid"
                codex_path="$counterpart"
                codex_oid="$counterpart_oid"
            else
                claude_path="$counterpart"
                claude_oid="$counterpart_oid"
                codex_path="$path"
                codex_oid="$path_oid"
            fi
            claude_file="$compare_tmp/index/$claude_path"
            codex_file="$compare_tmp/index/$codex_path"

            # 同期済みの定義を 1 つに固定する: ペアは互いの変換結果である
            # （Claude 側を変換すると Codex 側に一致し、かつ Codex 側を変換すると
            # Claude 側に一致する）。これで十分な理由は、変換が全単射ではなく
            # 片方向だけでは一致が保証にならないため:
            #   - 変換はすでに変換後の表記に対しては無変換なので、Claude 表記が
            #     混入した Codex 側でも逆変換すると Claude 側に一致してしまう。
            #   - to_claude_agent は frontmatter の extras を dst 側から借りるので、
            #     extras を失った Claude 側でも逆変換の結果と一致してしまう。
            # どちらの化け方も残り 1 方向では再現できないため、2 方向が同時に
            # 成り立つのは両側が正準なペアである場合だけになる。条件を足したのでは
            # なく、比較の基準をパス族で固定した結果として 2 方向が必要になる。
            #
            # 変換関数は dst を上書きする。index へ展開した原本は以降の比較の
            # 基準なので、変換の出力は必ず別の場所へ書く。
            forward_file="$compare_tmp/forward/$codex_path"
            backward_file="$compare_tmp/backward/$claude_path"
            mkdir -p -- "${forward_file%/*}" "${backward_file%/*}" || return 1

            to_codex_agent_skill "$claude_file" "$forward_file" >/dev/null || return 1
            converted_oid="$(git hash-object "$forward_file")" || return 1
            [[ "$converted_oid" == "$codex_oid" ]] || return 1

            # to_claude_agent は dst の frontmatter から model/color/tools を
            # 引き継ぐ。Claude 側の原本の複製を dst に置くことで、その引き継ぎ込みで
            # 元の内容が再現されるかを見る。
            git cat-file blob "$claude_oid" > "$backward_file" || return 1
            to_claude_agent "$codex_file" "$backward_file" >/dev/null || return 1
            converted_oid="$(git hash-object "$backward_file")" || return 1
            [[ "$converted_oid" == "$claude_oid" ]]
            ;;
        *)
            return 1
            ;;
    esac
}

stage_counterpart_if_present() {
    local counterpart="$1"
    local rc

    if [[ -e "$counterpart" || -L "$counterpart" ]]; then
        git add -A -- "$counterpart"
        return
    fi

    if read_index_entries "$counterpart"; then
        git add -A -- "$counterpart"
        return
    else
        rc=$?
    fi

    # A never-created counterpart needs no staging. A git failure is fatal.
    [[ "$rc" -eq 1 ]]
}

collect_state() {
    local staged_paths unmerged_paths unmerged rc identity_family pair_mode pair_oid

    declare -gA staged_set=()
    declare -gA issues=()
    declare -g has_claude_changes=0
    declare -g has_codex_changes=0

    if ! staged_paths="$(git diff --cached --name-only --diff-filter=ACMRDT)"; then
        return 1
    fi
    # Unmerged paths are not part of the normal staged-change set above, but must
    # still reach the fail-closed index validation below.
    if ! unmerged_paths="$(git diff --cached --name-only --diff-filter=U)"; then
        return 1
    fi
    if [[ -n "$unmerged_paths" ]]; then
        staged_paths+=$'\n'"$unmerged_paths"
    fi

    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        staged_set["$path"]=1

        # map_counterpart が値を返すパス族をすべて覆うこと。
        # 覆えていないパス族があると、issue はあるのにどちらのフラグも立たない。
        case "$path" in
            .claude/skills/*|.claude/agents/*)
                has_claude_changes=1
                ;;
            .codex/skills/*)
                has_codex_changes=1
                ;;
        esac
    done <<< "$staged_paths"

    for path in "${!staged_set[@]}"; do
        counterpart="$(map_counterpart "$path" || true)"
        [[ -n "$counterpart" ]] || continue

        if ! unmerged="$(git ls-files --unmerged -- "$path")"; then
            issues["$path"]="resolve and stage $path"
            continue
        fi
        if [[ -n "$unmerged" ]]; then
            issues["$path"]="resolve and stage $path"
            continue
        fi

        # 両側がステージされていても内容が食い違っていることはある（編集 →
        # 同期 → もう一度編集、の順で git add -A すると起きる）。ステージの
        # 有無だけで打ち切ると古いミラーがそのままコミットへ入るので、
        # 内容比較まで落とす。
        #
        # ただし恒等変換のパス族に限り、mode と oid が完全に一致するペアを
        # ここで同期済みとして確定させる。counterparts_match は通常ファイルの
        # テキストしか比較できず、両側で同じ型・同じ内容へミラーされた symlink の
        # ような変更を判定不能として弾いてしまうため。
        #
        # map_counterpart が値を返すパス族は 4 つあり、短絡してよいのは同期が
        # cp -R の恒等コピーである skills ↔ skills の 2 族だけ。agents ↔ エージェント
        # 派生スキルの 2 族は変換が恒等でない（フロントマターの model/color/tools を
        # 落とす・~/.claude/ を絶対パスへ・@agent-name をバッククォート形式へ）ため、
        # byte 一致はむしろ「変換を通していない＝未同期」の証拠になる。これらは常に
        # counterparts_match の内容比較へ落とす。
        # 短絡の本来の目的（型変更のミラー）は恒等コピーの族でしか成立しないので、
        # 族を限定しても目的は損なわれない。
        case "$path:$counterpart" in
            .claude/skills/*:.codex/skills/*|.codex/skills/*:.claude/skills/*)
                identity_family=1
                ;;
            *)
                identity_family=0
                ;;
        esac

        if [[ "$identity_family" -eq 1 ]] \
            && [[ -n "${staged_set[$counterpart]+x}" ]] \
            && read_stage_entry "$path"; then
            pair_mode="$stage_mode"
            pair_oid="$stage_oid"

            if read_stage_entry "$counterpart" \
                && [[ "$stage_mode" == "$pair_mode" && "$stage_oid" == "$pair_oid" ]]; then
                continue
            fi
        fi

        # A deleted source is synchronized only when its counterpart is also absent
        # from the index. A worktree-only deletion is not part of the next commit.
        if ! git diff --cached --quiet --diff-filter=D -- "$path"; then
            if read_index_entries "$counterpart"; then
                issues["$path"]="delete and stage $counterpart"
                continue
            else
                rc=$?
            fi
            if [[ "$rc" -eq 1 ]]; then
                continue
            fi
            issues["$path"]="delete and stage $counterpart"
            continue
        fi

        # Compare the staged blobs themselves. Looking only for worktree changes let
        # an old counterpart left at HEAD pass as synchronized.
        if read_index_entries "$counterpart"; then
            if ! counterparts_match "$path" "$counterpart"; then
                issues["$path"]="update and stage $counterpart"
            fi
        else
            rc=$?
            if [[ "$rc" -eq 2 ]]; then
                issues["$path"]="update and stage $counterpart"
                continue
            fi
            if [[ -e "$counterpart" || -L "$counterpart" ]]; then
                issues["$path"]="stage $counterpart"
            else
                issues["$path"]="create and stage $counterpart"
            fi
        fi
    done
}

collect_state

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

# collect_state を再実行すると has_claude_changes / has_codex_changes には
# このスクリプト自身の git add の結果が混ざり、片側同期の後でも「両側ステージ」に
# 見えてしまう。同期を起動したかどうかは再集計で壊れない別のフラグで覚えておく。
#
# 失敗時の else は「利用者が最初から両側をステージした」場合にだけ到達する。これは
# map_counterpart が値を返すパス族が collect_state のフラグ設定 case の部分集合で
# あることに依存している。この不変条件が崩れると、どちらのフラグも立たないまま
# else へ落ち、同期をスキップした覚えのない状況で「両側ステージだから曖昧」という
# 誤った説明が出る。
sync_ran=0

if [[ "$auto_sync" -eq 1 && "$has_claude_changes" -eq 1 && "$has_codex_changes" -eq 0 ]]; then
    echo "Auto-syncing skills from Claude to Codex..."
    "$sync_script" --from claude
    sync_ran=1
    for path in "${!issues[@]}"; do
        counterpart="$(map_counterpart "$path" || true)"
        if [[ -n "$counterpart" ]]; then
            stage_counterpart_if_present "$counterpart" || exit 1
        fi
    done
    collect_state
elif [[ "$auto_sync" -eq 1 && "$has_codex_changes" -eq 1 && "$has_claude_changes" -eq 0 ]]; then
    echo "Auto-syncing skills from Codex to Claude..."
    "$sync_script" --from codex
    sync_ran=1
    for path in "${!issues[@]}"; do
        counterpart="$(map_counterpart "$path" || true)"
        if [[ -n "$counterpart" ]]; then
            stage_counterpart_if_present "$counterpart" || exit 1
        fi
    done
    collect_state
fi

if [[ "${#issues[@]}" -eq 0 ]]; then
    exit 0
fi

echo "Skill/agent sync check failed."

if [[ "$auto_sync" -eq 0 ]]; then
    echo "This run only inspected the staged files; nothing was written or staged."
elif [[ "$sync_ran" -eq 1 ]]; then
    echo "Automatic sync ran, but staged files are still inconsistent."
else
    echo "Both Claude and Codex skill/agent files are staged in the same commit."
    echo "Automatic sync is skipped because the source of truth is ambiguous."
fi

echo "Remaining paths that need manual attention:"

for path in $(printf '%s\n' "${!issues[@]}" | sort); do
    echo "  - $path"
    echo "    ${issues[$path]}"
done

if [[ "$auto_sync" -eq 0 ]]; then
    echo
    echo "Let this script sync and stage the counterparts for you:"
    echo "  scripts/check-skill-sync.sh --auto-sync"
fi

echo
echo "Manual sync commands:"
echo "  scripts/sync-claude-codex-skills.sh --from claude"
echo "  scripts/sync-claude-codex-skills.sh --from codex"
echo
echo "If this is intentional, bypass once with:"
echo "  SKIP_SKILL_SYNC_CHECK=1 git commit ..."

exit 1
