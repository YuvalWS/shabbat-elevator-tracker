# shellcheck shell=sh
# Login banner for the Shabbat Elevator Tracker container (installed in /etc/profile.d).
case $- in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac
_set_src=/opt/shabbat-elevator-tracker/current/SOURCE
if [ -r "$_set_src" ]; then
  _set_v=$(sed -n 's/^version=//p' "$_set_src")
  _set_c=$(sed -n 's/^commit=//p' "$_set_src" | cut -c1-7)
  _set_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  _set_port=$(sed -n 's/^  port: *\([0-9]*\).*/\1/p' /etc/shabbat-elevator-tracker/config.yaml 2>/dev/null | head -1)
  printf '\n  Shabbat Elevator Tracker %s (%s)  http://%s:%s/\n' "$_set_v" "${_set_c:-local}" "${_set_ip:-?}" "${_set_port:-8080}"
  printf '  update             install the latest version\n'
  printf '  update --rollback  go back to the previous version\n'
  printf '  journalctl -u shabbat-elevator-tracker -f     logs\n\n'
fi
unset _set_src _set_v _set_c _set_ip _set_port
