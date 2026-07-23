#!/bin/sh -p
case $- in
    *p*)
        unset DEVELOPER_DIR SDKROOT TOOLCHAINS XCRUN_CACHE_PATH \
            DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH \
            DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH \
            DYLD_FORCE_FLAT_NAMESPACE DYLD_PRINT_TO_FILE \
            LD_PRELOAD LD_LIBRARY_PATH \
            PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE \
            PYTHONINSPECT PYTHONWARNINGS PYTHONBREAKPOINT PYTHONSAFEPATH \
            PYTHONPLATLIBDIR PYTHONEXECUTABLE _PYTHON_SYSCONFIGDATA_NAME \
            __PYVENV_LAUNCHER__ BASH_ENV ENV PS4 BASH_XTRACEFD \
            xcrun_log xcrun_nocache xcrun_verbose
        case $0 in
            */*) exec /usr/bin/python3 -I "${0%/*}/sidecar_launcher.py" build "$@" ;;
            *) /usr/bin/false ;;
        esac
        ;;
    *) /usr/bin/false ;;
esac
