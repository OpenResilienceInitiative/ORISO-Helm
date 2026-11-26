# Shared by the Pre-Dev lane scripts. Runs on the Pre-Dev server as root, started by
# ORISO-Frontend .github/workflows/frontend-deploy.yml (component predev-lane) from the
# pre-dev branch. Never print values files, secrets or rendered Secrets: run logs are public.
set -euo pipefail
LANE=/root/predev-lane
CHART=$LANE/ORISO-Helm
# Pre-Dev's own values and secrets, kept by the operators next to the old release checkout.
SERVER_VALUES=/root/ORISO-Helm/values.yaml
SERVER_SECRETS=/root/ORISO-Helm/secrets.yaml
# Values the lane adds (generated keys, add-on switch): one file per Helm key in $EXTRA,
# mode 600, never printed. <key>.bool -> --set, <key>.file -> --set-file, else --set-string.
EXTRA=$LANE/extra
RELEASE=caritas
NS=caritas

mkdir -p "$LANE" "$EXTRA"
chmod 700 "$LANE" "$EXTRA"

update_chart() {
	if [ ! -d "$CHART/.git" ]; then
		git clone -q --branch pre-dev https://github.com/OpenResilienceInitiative/ORISO-Helm "$CHART"
	fi
	git -C "$CHART" fetch -q origin pre-dev
	git -C "$CHART" checkout -q -B pre-dev origin/pre-dev
	echo "chart: $(git -C "$CHART" log -1 --format='%h %cd %s' --date=short)"
}

# Layering from values-pre-dev.yaml: values -> dev -> pre-dev -> secrets, then the lane extras.
values_args() {
	echo -n "-f $SERVER_VALUES -f $CHART/values-dev.yaml -f $CHART/values-pre-dev.yaml -f $SERVER_SECRETS"
	local f key
	for f in "$EXTRA"/*; do
		[ -e "$f" ] || continue
		key=$(basename "$f")
		case "$key" in
			*.bool) echo -n " --set ${key%.bool}=$(cat "$f")" ;;
			*.file) echo -n " --set-file ${key%.file}=$f" ;;
			*) echo -n " --set-string $key=$(cat "$f")" ;;
		esac
	done
}

set_extra() { # set_extra <helm.key[.bool|.file]> <value>
	install -m 600 /dev/null "$EXTRA/$1"
	printf '%s' "$2" > "$EXTRA/$1"
}

has_extra() { [ -s "$EXTRA/$1" ]; }
