#!/usr/bin/env bash
# skillhub-sync.sh - platform bootstrap for skill_sync.py (Linux / macOS).
#
# Fetches the skillhub-sync source over HTTPS (GitHub tarball API) so the tool
# still works on networks where `git clone` to github.com is blocked, then runs
# skill_sync.py with the requested mode. An access token, when present in the
# environment, is used only inside an HTTP header; it is never echoed, logged,
# written to disk, or placed in argv. TLS verification is never disabled.
#
# Exit codes: 0 ok, 2 attention needed, 3 config error, 4 apply failure.
# Codes 2-4 come from skill_sync.py; this wrapper itself only emits 3.

set -eu

PROG=${0##*/}

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 3; }
say() { printf '%s\n' "$*" >&2; }

usage() {
cat <<'EOF'
Usage: skillhub-sync.sh --profile NAME [options]

Bootstrap skill_sync.py from the skillhub-sync repository and run it.

Options:
  -h, --help            Show this help and exit.
      --repo-url URL    Repository to bootstrap.
                        Default: https://github.com/zjashanda/skillhub-sync.git
      --ref REF         Branch or tag to fetch. Default: main
      --dest DIR        Where the source checkout lives.
                        Default: ${XDG_DATA_HOME:-$HOME/.local/share}/ark/skillhub-sync
      --profile NAME    Manifest profile to sync (required unless --check).
      --manifest PATH   Manifest file.
                        Default: <dest>/manifests/canonical.json
      --mode MODE       status | plan | apply | verify. Default: plan
      --install-root PATH
                        Install root passed through to skill_sync.py.
      --token-env NAME  Environment variable holding the access token.
                        Default: GITHUB_TOKEN
      --via TRANSPORT   auto | http | git. Default: auto
      --python CMD      Python interpreter. Default: python3
      --force           Re-download even if <dest> already holds a checkout.
                        Refused when <dest> is a git checkout with local changes.
      --yes             Allow 'apply' to write; without it apply is a dry run.
      --check           Verify python/tar/curl/git, print resolved settings, exit 0.

The token is read from the named environment variable only. When the variable
is empty no Authorization header is sent at all. Never pass a token on the
command line: it would be visible in the process list.

Exit codes: 0 ok, 2 attention needed, 3 config error, 4 apply failure.
EOF
}

repo_url=https://github.com/zjashanda/skillhub-sync.git
ref=main
dest=${XDG_DATA_HOME:-$HOME/.local/share}/ark/skillhub-sync
profile=''
manifest=''
mode=plan
install_root=''
token_env=GITHUB_TOKEN
via=auto
python=python3
force=0
assume_yes=0
check_only=0

while [ $# -gt 0 ]; do
    opt=$1
    case $opt in
        -h | --help) usage; exit 0 ;;
        --force) force=1; shift; continue ;;
        --yes) assume_yes=1; shift; continue ;;
        --check) check_only=1; shift; continue ;;
        --repo-url | --ref | --dest | --profile | --manifest | --mode \
            | --install-root | --token-env | --via | --python) ;;
        -*) die "unknown option: $opt (try --help)" ;;
        *) die "unexpected argument: $opt (try --help)" ;;
    esac
    [ $# -ge 2 ] || die "missing value for $opt"
    case $opt in
        --repo-url) repo_url=$2 ;;
        --ref) ref=$2 ;;
        --dest) dest=$2 ;;
        --profile) profile=$2 ;;
        --manifest) manifest=$2 ;;
        --mode) mode=$2 ;;
        --install-root) install_root=$2 ;;
        --token-env) token_env=$2 ;;
        --via) via=$2 ;;
        --python) python=$2 ;;
    esac
    shift 2
done

case $mode in
    status | plan | apply | verify) ;;
    *) die "invalid --mode '$mode' (want status|plan|apply|verify)" ;;
esac
case $via in
    auto | http | git) ;;
    *) die "invalid --via '$via' (want auto|http|git)" ;;
esac

[ -n "$profile" ] || [ "$check_only" -eq 1 ] || die "--profile is required (or use --check)"

[ -n "$dest" ] || die "--dest must not be empty"
dest=${dest%/}
[ -n "$dest" ] || die "--dest must not be the filesystem root"
case $dest in
    /*) ;;
    *) dest=$PWD/$dest ;;
esac
[ -n "$manifest" ] || manifest=$dest/manifests/canonical.json

# Read the token through printenv: the value never becomes part of a command
# line and is not exported to child processes.
token=''
if token_value=$(printenv "$token_env" 2>/dev/null); then
    token=$token_value
    unset token_value
fi
if [ -n "$token" ]; then token_state=present; else token_state=absent; fi

if [ "$check_only" -eq 1 ]; then
    python_path=$(command -v "$python" 2>/dev/null || true)
    [ -n "$python_path" ] || die "python not found: $python"
    printf 'skillhub-sync bootstrap check\n'
    printf '  python:   %s (%s)\n' "$python" "$python_path"
    for tool in git curl tar; do
        tool_path=$(command -v "$tool" 2>/dev/null || true)
        if [ -n "$tool_path" ]; then
            printf '  %-8s  %s\n' "$tool:" "$tool_path"
        else
            printf '  %-8s  missing\n' "$tool:"
        fi
    done
    printf '  dest:     %s\n' "$dest"
    printf '  ref:      %s\n' "$ref"
    printf '  mode:     %s\n' "$mode"
    printf '  via:      %s\n' "$via"
    printf '  manifest: %s\n' "$manifest"
    printf '  token:    %s=%s\n' "$token_env" "$token_state"
    exit 0
fi

command -v "$python" >/dev/null 2>&1 || die "python not found: $python"

needs_fetch=1
if [ "$force" -eq 0 ] && [ -f "$dest/skill_sync.py" ]; then
    needs_fetch=0
fi

tmp=''
cleanup() {
    if [ -n "$tmp" ]; then
        rm -rf "$tmp"
    fi
}

# Refuse to delete a checkout that holds work we cannot reproduce.
assert_removable() {
    [ -e "$dest" ] || return 0
    [ -d "$dest/.git" ] || return 0
    command -v git >/dev/null 2>&1 ||
        die "refusing to replace $dest: it is a git checkout and git is unavailable to verify it is clean"
    dirty=$(git -C "$dest" --no-optional-locks status --porcelain 2>/dev/null || true)
    [ -z "$dirty" ] ||
        die "refusing to replace $dest: it has local modifications (commit or stash them first)"
    return 0
}

install_dir() {
    src_dir=$1
    [ -f "$src_dir/skill_sync.py" ] || die "downloaded source has no skill_sync.py: $src_dir"
    if [ -e "$dest" ]; then
        assert_removable
        rm -rf "$dest"
    fi
    mv "$src_dir" "$dest"
}

fetch_http() {
    [ -n "$slug" ] || die "cannot derive <owner>/<repo> from --repo-url '$repo_url'; use --via git"
    command -v curl >/dev/null 2>&1 || die "curl is required for the HTTPS tarball transport"
    command -v tar >/dev/null 2>&1 || die "tar is required to unpack the HTTPS tarball"
    url=https://api.github.com/repos/$slug/tarball/$ref
    tgz=$tmp/source.tar.gz
    say "  fetching $url"
    if [ -n "$token" ]; then
        # Header arrives on stdin so the token never shows up in argv.
        printf 'header = "Authorization: Bearer %s"\n' "$token" |
            curl -fsSL -K - -o "$tgz" "$url"
    else
        curl -fsSL -o "$tgz" "$url"
    fi
    mkdir -p "$tmp/x"
    tar -xzf "$tgz" -C "$tmp/x" || return 1
    top=''
    count=0
    for entry in "$tmp/x"/*; do
        [ -e "$entry" ] || continue
        top=$entry
        count=$((count + 1))
    done
    [ "$count" -eq 1 ] || die "unexpected archive layout: want one top-level dir, found $count"
    install_dir "$top"
}

fetch_git() {
    command -v git >/dev/null 2>&1 ||
        die "git is required for the git transport but was not found"
    say "  cloning $repo_url ($ref)"
    git clone --depth 1 --branch "$ref" -- "$repo_url" "$tmp/clone"
    install_dir "$tmp/clone"
}

# Fail before downloading anything if --force would clobber local work.
if [ "$force" -eq 1 ] && [ -e "$dest" ]; then
    assert_removable
fi

say "skillhub-sync bootstrap"
say "  dest:    $dest"
say "  ref:     $ref"
say "  mode:    $mode"
say "  via:     $via"
say "  token:   $token_env=$token_state"

if [ "$needs_fetch" -eq 1 ]; then
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    mkdir -p "$(dirname "$dest")"
    tmp=$dest.tmp.$$
    rm -rf "$tmp"
    mkdir -p "$tmp"

    case $repo_url in
        file://* | /* | ./* | ../* | '~'*) is_local=1 ;;
        *) is_local=0 ;;
    esac

    # Accept https://github.com/<owner>/<repo>[.git]; anything else needs git.
    slug=''
    case $repo_url in
        https://github.com/*/*)
            slug=${repo_url#https://github.com/}
            slug=${slug%.git}
            case $slug in
                */*/* | '') slug='' ;;
            esac
            ;;
    esac

    if [ "$is_local" -eq 1 ]; then
        [ "$via" != http ] ||
            die "--via http cannot fetch a local path; use auto or git"
        fetch_git
    else
        case $via in
            git) fetch_git ;;
            http) fetch_http || die "HTTPS tarball download failed; retry with --via git" ;;
            auto)
                if ! fetch_http; then
                    say "  HTTPS tarball failed; falling back to git clone"
                    fetch_git
                fi
                ;;
        esac
    fi
else
    say "  using existing checkout"
fi

[ -f "$dest/skill_sync.py" ] || die "bootstrap incomplete: $dest/skill_sync.py is missing"

set -- "$dest/skill_sync.py" --manifest "$manifest" --profile "$profile" --transport "$via" "$mode"
if [ -n "$install_root" ]; then
    set -- "$@" --install-root "$install_root"
fi
if [ "$mode" = apply ] && [ "$assume_yes" -eq 1 ]; then
    set -- "$@" --yes
fi

cleanup
tmp=''
trap - EXIT
say "  running: $python $dest/skill_sync.py ..."
exec "$python" "$@"
