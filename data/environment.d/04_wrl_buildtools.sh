# Copyright (C) 2016-2019 Wind River Systems, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

# Download, install and load the buildtools tarball (as needed)

BUILDTOOLS_VERSION=${BUILDTOOLS_VERSION:-10.22.33.6b}
BUILDTOOLS_EXT_VERSION=${BUILDTOOLS_EXT_VERSION:-10.22.33.6b}

# Special windshare folders to search
BUILDTOOLS_FOLDERS="WRLinux-CD-Core"


# Where to install the build tools
BUILDTOOLS="${BUILDTOOLS:-bin/buildtools}"

# Arch of the SDK to load
SDKARCH=${SDKARCH:-$(uname -m)}

# buildtools is only available for x86_64
if [ "$SDKARCH" != "x86_64" ]; then
    echo "WARNING: buildtools is disabled for $SDKARCH" >&2
    return 0
fi

setup_add_arg --buildtools-branch BUILDTOOLSBRANCH keep

setup_add_arg --buildtools-type BUILDTOOLS_TYPE keep

setup_add_func buildtools_setup

setup_export_func buildtools_export

. ${BASEDIR}/data/environment.d/setup_utils

USE_BUILDTOOLS_CERT=false

BINTOOLS_SSL_DIR="/sysroots/x86_64-wrlinuxsdk-linux/usr/share/ca-certificates/mozilla"
BINTOOLS_SSL_CERT="/sysroots/x86_64-wrlinuxsdk-linux/etc/ssl/certs/ca-certificates.crt"

CERT_SUFFIX=".buildtools"

# linking buildtools cert to host cert store, only handle current
# supported distro
link_host_cert_store(){
	capath=$1
	cafile=$2
	if [ ! -e "$cafile$CERT_SUFFIX" ]; then
		linked=false
		#fedora like distro
		if [ -d /etc/pki/tls/certs ] && [ -e /etc/pki/tls/certs/ca-bundle.crt ]; then
			mv "$cafile" "$cafile$CERT_SUFFIX"
			mv "$capath" "$capath$CERT_SUFFIX"
			ln -sf /etc/pki/tls/certs "$capath"
			ln -sf /etc/pki/tls/certs/ca-bundle.crt "$cafile"
			linked=true
		#debian like distro
		elif [ -d /etc/ssl/certs ] && [ -e /etc/ssl/certs/ca-certificates.crt ]; then
			mv "$cafile" "$cafile$CERT_SUFFIX"
			mv "$capath" "$capath$CERT_SUFFIX"
			ln -sf /etc/ssl/certs "$capath"
			ln -sf /etc/ssl/certs/ca-certificates.crt "$cafile"
			linked=true
		#opensuse
		elif [ -d /etc/ssl/certs ] && [ -e /etc/ssl/ca-bundle.pem ];then
			mv "$cafile" "$cafile$CERT_SUFFIX"
			mv "$capath" "$capath$CERT_SUFFIX"
			ln -sf /etc/ssl/certs "$capath"
			ln -sf /etc/ssl/ca-bundle.pem "$cafile"
			linked=true
		else
			echo "Valid host cert not found, still using buildtools cert"
		fi
		if [ "$linked" = "true" ]; then
			OPENSSL_ENVFILE="${BUILDTOOLS}/sysroots/x86_64-wrlinuxsdk-linux/environment-setup.d/openssl.sh"
			if [ -f ${OPENSSL_ENVFILE} ]; then
				sed -i -e "/SSL_CERT_DIR/d" ${OPENSSL_ENVFILE}
				sed -i -e "/SSL_CERT_FILE/d" ${OPENSSL_ENVFILE}
				echo "export SSL_CERT_FILE=\$OECORE_NATIVE_SYSROOT/etc/ssl/certs/ca-certificates.crt" >> ${OPENSSL_ENVFILE}
				echo "export SSL_CERT_DIR=\$OECORE_NATIVE_SYSROOT/usr/share/ca-certificates/mozilla" >> ${OPENSSL_ENVFILE}
			fi
		fi
	fi
}

# restore host certs store to buildtools cert
restore_buildtools_cert() {
	capath=$1
	cafile=$2
	if [ -f "$cafile$CERT_SUFFIX" ]; then
		echo "Restoring host certs to buildtools cert"
		rm -rf "$capath"
		rm -rf "$cafile"
		mv "$cafile$CERT_SUFFIX" "$cafile"
		mv "$capath$CERT_SUFFIX" "$capath"
	fi
}

buildtools_setup() {
	if [ -z "${BUILDTOOLSBRANCH}" ]; then
		BUILDTOOLSBRANCH="${BASEBRANCH}"
	fi

	#qt6 require at least gcc9+
	gcc_cur_ver=$(gcc -dumpfullversion -dumpversion 2>/dev/null)
	required_ver=9.3.1

	# check whether host gcc version less than $required_ver
	if [ ! "$(printf '%s\n' "$required_ver" "$gcc_cur_ver" | sort -V | head -n1)" = "$required_ver" ]; then
		if [ basic = "${BUILDTOOLS_TYPE}" ]; then
			echo -e "\nWarning: The version of host gcc is too low to support c++14 standard. It may cause some packages such as doxygen-native fail to build with basic buildtools.\n"
		fi

		if [ -z "${BUILDTOOLS_TYPE}" ]; then
			BUILDTOOLS_TYPE=extended
		fi
	fi

	# On the build server with older gcc, rerun setup may make
	# buildtools different from the first run, check if gcc is
	# provided by extended buildtools to fix the gap
	if [ $(which gcc 2>&1 | grep "buildtools-extended-standalone") ] && [ -z "${BUILDTOOLS_TYPE}" ]; then
		BUILDTOOLS_TYPE=extended
	fi

	if [ -z "${BUILDTOOLS_TYPE}" ]; then
		BUILDTOOLS_TYPE=basic
	fi

	if [ basic != "${BUILDTOOLS_TYPE}" ] && [ extended != "${BUILDTOOLS_TYPE}" ]; then
		echo "Wrong argument \"${BUILDTOOLS_TYPE}\" for option --buildtools-type. Supported arguments: basic, extended." >&2
		return 1
	fi

	if [ basic = "${BUILDTOOLS_TYPE}" ]; then
		buildtools=buildtools
		buildtools_version=$BUILDTOOLS_VERSION
	else
		buildtools=buildtools-extended
		buildtools_version=$BUILDTOOLS_EXT_VERSION
	fi

	# Buildtools location can change -- this is the path on top of the BASEURL
	BUILDTOOLS_REMOTE="${BUILDTOOLS_REMOTE:-${buildtools}-standalone-${buildtools_version}}"

	# Where to cache the git fetch
	BUILDTOOLS_GIT="${BUILDTOOLS_GIT:-bin/${buildtools}.git}"

	FETCH_BUILDTOOLS=0

	# Install them into the project directory
	EXTRACT_BUILDTOOLS=0

	BUILDTOOLS_REF=$(echo ${BUILDTOOLS_REMOTE} | sed -e 's,.*/buildtools-standalone-,,')

	if [ ! -d "${BUILDTOOLS_GIT}" ]; then
		FETCH_BUILDTOOLS=1

		(mkdir -p ${BUILDTOOLS_GIT} && git init ${BUILDTOOLS_GIT})
		if [ $? -ne 0 ]; then
			echo "Unable to create ${BUILDTOOLS_GIT} directory." >&2
			return 1
		fi
	else
		# Did the buildtools URL change?
		LASTREF=$(git config -f ${BUILDTOOLS_GIT}/.git/config local.last.ref)
		if [ "${LASTREF}" != "${BUILDTOOLS_REF}" ]; then
			FETCH_BUILDTOOLS=1
		fi
	fi

	if [ ${FETCH_BUILDTOOLS} -ne 1 ]; then
		# We need this in order to have the right path for subsequent mirror operations
		BUILDTOOLS_REMOTE=$(git config -f ${BUILDTOOLS_GIT}/.git/config local.${BUILDTOOLS_REF}.path)
	else
		echo "Searching for ${BUILDTOOLS_REMOTE}..."

		retries=0
		duration=5
		for i in {1..5} ; do
			if ! setup_check_url "${BASEURL}/${BUILDTOOLS_REMOTE}" ; then
				ORIG_BT_REMOTE=${BUILDTOOLS_REMOTE}
				# Additional places to search...
				for folder in ${BUILDTOOLS_FOLDERS} layers/buildtools; do
					NEW_REMOTE=${folder}/${BUILDTOOLS_REMOTE}
					if setup_check_url "${BASEURL}/${NEW_REMOTE}" ; then
						BUILDTOOLS_REMOTE=${NEW_REMOTE}
					fi
				done
				if [ "${BUILDTOOLS_REMOTE}" = "${ORIG_BT_REMOTE}" ]; then
					retries=$(($retries+1))
					echo "Retrying $1 after $duration seconds -- $retries time(s) (max: 5)"
					sleep $duration
					duration=$(($duration+$(random 1 5)))
				fi
			else
				break
			fi
		done

		if [ $retries -eq 5 ]; then
			echo "Unable to find ${BUILDTOOLS_REMOTE}.  Search path:">&2
			for folder in ${BUILDTOOLS_FOLDERS} layers/buildtools; do
				echo " ${BASEURL}/${folder}/${BUILDTOOLS_REMOTE}" >&2
			done
			return 1
		fi

		echo "Fetching buildtools.."
		# Check if it's a tag
		if [ "$BASEBRANCH" != "${BASEBRANCH##refs/tags/}" ]; then
			local_name="${BUILDTOOLSBRANCH}:tags/${BUILDTOOLS_REF}"
		else
			local_name="${BUILDTOOLSBRANCH}:${BUILDTOOLS_REF}"
		fi
		trap : INT
		retries=0
		duration=5
		ret=0
		for i in {1..5} ; do
			echo "${BASEURL}/${BUILDTOOLS_REMOTE}"
			(cd ${BUILDTOOLS_GIT} && git fetch -f -n -u "${BASEURL}/${BUILDTOOLS_REMOTE}" $local_name)
			ret=$?
			if [ $ret -eq 0 ] || [ $ret -eq 130 ]; then
				break
			else
				retries=$(($retries+1))
				echo "Retrying $1 after $duration seconds -- $retries time(s) (max: 5)"
				sleep $duration
				duration=$(($duration+$(random 1 5)))
			fi
		done

		if [ $retries -eq 5 ] || [ $ret -eq 130 ]; then
			echo "Error fetching buildtools repository ${BASEURL}/${BUILDTOOLS_REMOTE}" >&2
			return 1
		fi
		trap - INT
		# Set a flag so we know where the fetch was from...
		(
			cd ${BUILDTOOLS_GIT}
			git config "local.${BUILDTOOLS_REF}.url" "${BASEURL}/${BUILDTOOLS_REMOTE}"
			git config "local.${BUILDTOOLS_REF}.path" "${BUILDTOOLS_REMOTE}"
			git config local.last.ref "${BUILDTOOLS_REF}"
			git checkout "${BUILDTOOLS_REF}"
		)
		if [ $? -ne 0 ]; then
			echo "Unable to checkout branch ${BUILDTOOLS_REF}." >&2
			return 1
		fi
		echo "Done"

		EXTRACT_BUILDTOOLS=1
	fi

	if [ ! -d "${BUILDTOOLS}.${BUILDTOOLS_REF}" ]; then
		EXTRACT_BUILDTOOLS=1
	fi

	if [ ${EXTRACT_BUILDTOOLS} -ne 1 ]; then
		ENVIRON=$(find -L ${BUILDTOOLS} -name "environment-setup-${SDKARCH}-*-linux" | head -n1)
		if [ -z "${ENVIRON}" ]; then
			# Something is wrong, try to fix it!
			EXTRACT_BUILDTOOLS=1
		fi
	fi

	if [ ${EXTRACT_BUILDTOOLS} -eq 1 ]; then
		# Needs python.
		buildtoolssdk=$(find "${BUILDTOOLS_GIT}" -name "${SDKARCH}-${buildtools}-nativesdk-standalone-*.sh" 2>/dev/null | sort | head -n1)
		buildtoolssdk_list=$(find "${BUILDTOOLS_GIT}" -name "${SDKARCH}-${buildtools}-nativesdk-standalone-*.list" 2>/dev/null | sort | head -n1)
		if [ -z "${buildtoolssdk_list}" ]; then
			if [ -z "${buildtoolssdk}" ]; then
				echo "Unable to find buildtools-nativesdk-standalone archive for ${SDKARCH}." >&2
				echo >&2
				echo "SDKARCH values found:" >&2
				echo $(find "${BUILDTOOLS_GIT}" -name "*-${buildtools}-nativesdk-standalone-*.sh" | xargs -n 1 basename | cut -d '-' -f 1) >&2
				echo >&2
				echo "If one of these is compatible, set SDKARCH in your environment." >&2
				echo >&2
				return 1
			fi
		else
			buildtoolssdk=${buildtoolssdk_list/%.list/.sh}
			rm -f ${buildtoolssdk}
			for part in $(cat ${buildtoolssdk_list}); do
				cat ${BUILDTOOLS_GIT}/${part} >>${buildtoolssdk}
			done
			chmod +x ${buildtoolssdk}
		fi

		echo "Installing buildtools.."
		if [ -d "${BUILDTOOLS}.${BUILDTOOLS_REF}" ]; then
			rm -rf "${BUILDTOOLS}.${BUILDTOOLS_REF}"
		fi
		trap : INT
		${buildtoolssdk} -d "${BUILDTOOLS}.${BUILDTOOLS_REF}" -y
		if [ $? -ne 0 ]; then
			echo >&2
			echo "Error installing the buildtools-nativesdk-standalone archive: ${buildtoolssdk}" >&2
			# We try to cleanup, but an over zealous (sigint) user can stop the rm as well.
			rm -rf ${BUILDTOOLS}.${BUILDTOOLS_REF}
			return 1
		fi
		trap - INT
		echo "Done"
	fi

	# force to re-create the link that buildtools type may change
	rm -f ${BUILDTOOLS}
	ln -s $(basename ${BUILDTOOLS}).${BUILDTOOLS_REF} ${BUILDTOOLS}

	unset FETCH_BUILDTOOLS EXTRACT_BUILDTOOLS

	ENVIRON=$(find -L ${BUILDTOOLS} -name "environment-setup-${SDKARCH}-*-linux" | head -n1)
	if [ -z "${ENVIRON}" ]; then
		echo "Error unable to load buildtools environment-setup file." >&2
		return 1
	fi
    if [ "$USE_BUILDTOOLS_CERT" = "false" ]; then
        echo "Linking buildtools cert to host cert store."
        link_host_cert_store ${BUILDTOOLS}${BINTOOLS_SSL_DIR} ${BUILDTOOLS}${BINTOOLS_SSL_CERT}
    else
        restore_buildtools_cert ${BUILDTOOLS}${BINTOOLS_SSL_DIR} ${BUILDTOOLS}${BINTOOLS_SSL_CERT}
    fi
	. "${ENVIRON}"
	if [ $? -ne 0 ]; then
		echo "Unable to load the buildtools environment setup file." >&2
		return 1
	fi
	BUILDTOOLS_LOADED=1
	return 0
}


buildtools_export() {
	if [ -z "${BUILDTOOLSBRANCH}" ]; then
		BUILDTOOLSBRANCH="${BASEBRANCH}"
	fi

	export OE_BUILDTOOLS_BRANCH=${BUILDTOOLSBRANCH}
	export OE_BUILDTOOLS_REMOTE=${BUILDTOOLS_REMOTE}
	if [ basic = "${BUILDTOOLS_TYPE}" ]; then
		export OE_ANOTHER_BUILDTOOLS_REMOTE=`echo ${BUILDTOOLS_REMOTE} | sed -e "s,buildtools-standalone-,buildtools-extended-standalone-," \
			| sed -e "s,${BUILDTOOLS_VERSION},${BUILDTOOLS_EXT_VERSION},"`
	else
		export OE_ANOTHER_BUILDTOOLS_REMOTE=`echo ${BUILDTOOLS_REMOTE} | sed -e "s,buildtools-extended-standalone-,buildtools-standalone-," \
			| sed -e "s,${BUILDTOOLS_EXT_VERSION},${BUILDTOOLS_VERSION},"`
	fi
	return 0
}
