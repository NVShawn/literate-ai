#!/bin/sh

# Follow PATH directory order. Within each directory, prefer python3 over python.
remaining=${PATH-}
python_candidates='python3 python'
# FreeBSD packages expose versioned interpreters without the optional python3 meta port.
if [ "$(uname -s 2>/dev/null)" = FreeBSD ]; then
    python_candidates="$python_candidates python3.14 python3.13 python3.12 python3.11"
fi
resolver_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case $remaining in
    *';'*) separator=';' ;;
    *) separator=':' ;;
esac
while :; do
    final=true
    directory=$remaining
    if [ "$separator" = : ]; then
        case $remaining in
            *:*)
                directory=${remaining%%:*}
                remaining=${remaining#*:}
                final=false
                ;;
        esac
    else
        case $remaining in
            *';'*)
                directory=${remaining%%;*}
                remaining=${remaining#*;}
                final=false
                ;;
        esac
    fi
    if [ -z "$directory" ]; then
        directory=.
    fi
    for name in $python_candidates; do
        for suffix in '' .exe; do
            candidate=$directory/$name$suffix
            if [ -f "$candidate" ] && [ -x "$candidate" ] && \
                "$candidate" "$resolver_dir/python_resolver.py" --check \
                    >/dev/null 2>&1
            then
                printf '%s\n' "$candidate"
                exit 0
            fi
        done
    done
    if [ "$final" = true ]; then
        break
    fi
done

exit 1
