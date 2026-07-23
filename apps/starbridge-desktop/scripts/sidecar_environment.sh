#!/bin/sh

# Execute the Darwin sidecar Python entry point with a narrow, explicit
# pass-through environment. The caller supplies the trusted Python runner,
# script path, and original arguments.
sidecar_exec_clean() {
    SIDECAR_RUNNER=$1
    SIDECAR_SCRIPT=$2
    shift 2
    set -- "$SIDECAR_RUNNER" -I "$SIDECAR_SCRIPT" "$@"

    if [ "${CARGO_BUILD_TARGET+x}" = x ]; then
        set -- "CARGO_BUILD_TARGET=$CARGO_BUILD_TARGET" "$@"
    fi

    if [ "${PIP_INDEX_URL+x}" = x ]; then
        set -- "PIP_INDEX_URL=$PIP_INDEX_URL" "$@"
    fi
    if [ "${PIP_EXTRA_INDEX_URL+x}" = x ]; then
        set -- "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL" "$@"
    fi
    if [ "${PIP_NO_INDEX+x}" = x ]; then
        set -- "PIP_NO_INDEX=$PIP_NO_INDEX" "$@"
    fi
    if [ "${PIP_FIND_LINKS+x}" = x ]; then
        set -- "PIP_FIND_LINKS=$PIP_FIND_LINKS" "$@"
    fi
    if [ "${PIP_TRUSTED_HOST+x}" = x ]; then
        set -- "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST" "$@"
    fi
    if [ "${PIP_PROXY+x}" = x ]; then
        set -- "PIP_PROXY=$PIP_PROXY" "$@"
    fi
    if [ "${PIP_CERT+x}" = x ]; then
        set -- "PIP_CERT=$PIP_CERT" "$@"
    fi
    if [ "${PIP_CLIENT_CERT+x}" = x ]; then
        set -- "PIP_CLIENT_CERT=$PIP_CLIENT_CERT" "$@"
    fi
    if [ "${PIP_TIMEOUT+x}" = x ]; then
        set -- "PIP_TIMEOUT=$PIP_TIMEOUT" "$@"
    fi
    if [ "${PIP_DEFAULT_TIMEOUT+x}" = x ]; then
        set -- "PIP_DEFAULT_TIMEOUT=$PIP_DEFAULT_TIMEOUT" "$@"
    fi
    if [ "${PIP_RETRIES+x}" = x ]; then
        set -- "PIP_RETRIES=$PIP_RETRIES" "$@"
    fi
    if [ "${PIP_RESUME_RETRIES+x}" = x ]; then
        set -- "PIP_RESUME_RETRIES=$PIP_RESUME_RETRIES" "$@"
    fi
    if [ "${PIP_DISABLE_PIP_VERSION_CHECK+x}" = x ]; then
        set -- "PIP_DISABLE_PIP_VERSION_CHECK=$PIP_DISABLE_PIP_VERSION_CHECK" "$@"
    fi
    if [ "${PIP_NO_INPUT+x}" = x ]; then
        set -- "PIP_NO_INPUT=$PIP_NO_INPUT" "$@"
    fi
    if [ "${PIP_KEYRING_PROVIDER+x}" = x ]; then
        set -- "PIP_KEYRING_PROVIDER=$PIP_KEYRING_PROVIDER" "$@"
    fi
    if [ "${PIP_NETRC+x}" = x ]; then
        set -- "PIP_NETRC=$PIP_NETRC" "$@"
    fi
    if [ "${PIP_REQUIRE_VIRTUALENV+x}" = x ]; then
        set -- "PIP_REQUIRE_VIRTUALENV=$PIP_REQUIRE_VIRTUALENV" "$@"
    fi
    if [ "${PIP_REQUIRE_VENV+x}" = x ]; then
        set -- "PIP_REQUIRE_VENV=$PIP_REQUIRE_VENV" "$@"
    fi

    if [ "${HOME+x}" = x ]; then
        set -- "HOME=$HOME" "$@"
    fi
    if [ "${TMPDIR+x}" = x ]; then
        set -- "TMPDIR=$TMPDIR" "$@"
    fi
    if [ "${TMP+x}" = x ]; then
        set -- "TMP=$TMP" "$@"
    fi
    if [ "${TEMP+x}" = x ]; then
        set -- "TEMP=$TEMP" "$@"
    fi
    if [ "${LANG+x}" = x ]; then
        set -- "LANG=$LANG" "$@"
    fi
    if [ "${LANGUAGE+x}" = x ]; then
        set -- "LANGUAGE=$LANGUAGE" "$@"
    fi
    if [ "${LC_ADDRESS+x}" = x ]; then
        set -- "LC_ADDRESS=$LC_ADDRESS" "$@"
    fi
    if [ "${LC_ALL+x}" = x ]; then
        set -- "LC_ALL=$LC_ALL" "$@"
    fi
    if [ "${LC_COLLATE+x}" = x ]; then
        set -- "LC_COLLATE=$LC_COLLATE" "$@"
    fi
    if [ "${LC_CTYPE+x}" = x ]; then
        set -- "LC_CTYPE=$LC_CTYPE" "$@"
    fi
    if [ "${LC_IDENTIFICATION+x}" = x ]; then
        set -- "LC_IDENTIFICATION=$LC_IDENTIFICATION" "$@"
    fi
    if [ "${LC_MEASUREMENT+x}" = x ]; then
        set -- "LC_MEASUREMENT=$LC_MEASUREMENT" "$@"
    fi
    if [ "${LC_MESSAGES+x}" = x ]; then
        set -- "LC_MESSAGES=$LC_MESSAGES" "$@"
    fi
    if [ "${LC_MONETARY+x}" = x ]; then
        set -- "LC_MONETARY=$LC_MONETARY" "$@"
    fi
    if [ "${LC_NAME+x}" = x ]; then
        set -- "LC_NAME=$LC_NAME" "$@"
    fi
    if [ "${LC_NUMERIC+x}" = x ]; then
        set -- "LC_NUMERIC=$LC_NUMERIC" "$@"
    fi
    if [ "${LC_PAPER+x}" = x ]; then
        set -- "LC_PAPER=$LC_PAPER" "$@"
    fi
    if [ "${LC_TELEPHONE+x}" = x ]; then
        set -- "LC_TELEPHONE=$LC_TELEPHONE" "$@"
    fi
    if [ "${LC_TIME+x}" = x ]; then
        set -- "LC_TIME=$LC_TIME" "$@"
    fi
    if [ "${USER+x}" = x ]; then
        set -- "USER=$USER" "$@"
    fi
    if [ "${LOGNAME+x}" = x ]; then
        set -- "LOGNAME=$LOGNAME" "$@"
    fi
    if [ "${TZ+x}" = x ]; then
        set -- "TZ=$TZ" "$@"
    fi

    if [ "${HTTP_PROXY+x}" = x ]; then
        set -- "HTTP_PROXY=$HTTP_PROXY" "$@"
    fi
    if [ "${HTTPS_PROXY+x}" = x ]; then
        set -- "HTTPS_PROXY=$HTTPS_PROXY" "$@"
    fi
    if [ "${ALL_PROXY+x}" = x ]; then
        set -- "ALL_PROXY=$ALL_PROXY" "$@"
    fi
    if [ "${NO_PROXY+x}" = x ]; then
        set -- "NO_PROXY=$NO_PROXY" "$@"
    fi
    if [ "${FTP_PROXY+x}" = x ]; then
        set -- "FTP_PROXY=$FTP_PROXY" "$@"
    fi
    if [ "${http_proxy+x}" = x ]; then
        set -- "http_proxy=$http_proxy" "$@"
    fi
    if [ "${https_proxy+x}" = x ]; then
        set -- "https_proxy=$https_proxy" "$@"
    fi
    if [ "${all_proxy+x}" = x ]; then
        set -- "all_proxy=$all_proxy" "$@"
    fi
    if [ "${no_proxy+x}" = x ]; then
        set -- "no_proxy=$no_proxy" "$@"
    fi
    if [ "${ftp_proxy+x}" = x ]; then
        set -- "ftp_proxy=$ftp_proxy" "$@"
    fi
    if [ "${SSL_CERT_FILE+x}" = x ]; then
        set -- "SSL_CERT_FILE=$SSL_CERT_FILE" "$@"
    fi
    if [ "${SSL_CERT_DIR+x}" = x ]; then
        set -- "SSL_CERT_DIR=$SSL_CERT_DIR" "$@"
    fi
    if [ "${REQUESTS_CA_BUNDLE+x}" = x ]; then
        set -- "REQUESTS_CA_BUNDLE=$REQUESTS_CA_BUNDLE" "$@"
    fi
    if [ "${CURL_CA_BUNDLE+x}" = x ]; then
        set -- "CURL_CA_BUNDLE=$CURL_CA_BUNDLE" "$@"
    fi

    exec /usr/bin/env -i "PATH=/usr/bin:/bin:/usr/sbin:/sbin" "$@"
}
