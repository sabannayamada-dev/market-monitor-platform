set -eu
for target in \
    /tmp/market-monitor-fix-20260816_215132 \
    /tmp/market-monitor-fix-20260816_2212 \
    /tmp/patent-market-validation-20260816
do
    resolved=$(readlink -f -- "$target" 2>/dev/null || true)
    case "$resolved" in
        "$target") rm -rf -- "$resolved" ;;
        "") : ;;
        *) echo "refusing unexpected path: $resolved" >&2; exit 1 ;;
    esac
done
echo "remote temporary validation files removed"
