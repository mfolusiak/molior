#!/bin/bash

# echo $0 $@

usage()
{
  echo "Usage: $0 build|publish|remove <distrelease> <name> <version> <architecture> components <mirror url> keys" 1>&2
  echo "       $0 info" 1>&2
  exit 1
}

if [ "$1" == "build" -a "$#" -lt 7 ]; then
  usage
elif [ "$1" != "publish" -a "$#" -lt 4 ]; then
  usage
elif [ "$1" != "remove" -a "$#" -lt 4 ]; then
  usage
fi

ACTION=$1
DIST_RELEASE=$2
DIST_NAME=$3
DIST_VERSION=$4
ARCH=$5
COMPONENTS=$6 # separated by comma
REPO_URL=$7
KEYS="$8"  # separated by space

CHROOT_D=/var/lib/schroot/chroots/chroot.d
CHROOT_NAME="${DIST_NAME}-$DIST_VERSION-${ARCH}"
target="/var/lib/schroot/chroots/${CHROOT_NAME}"

set -e

build_chroot()
{
  rm -f $target.tar.xz
  rm -rf $target
  mkdir -p $target

  echo
  message="Creating schroot $CHROOT_NAME"
  echo "++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++"
  printf "| %-44s %s |\n" "$message" "`date -R`"
  echo "++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++"
  echo

  echo I: Using APT repository $REPO_URL

  if [ -n "$COMPONENTS" ]; then
      COMPONENTS="--components main,$COMPONENTS"
  fi
  # INCLUDE="--include=gnupg"

  keydir=`mktemp -d /tmp/molior-chrootkeys.XXXXXX`
  i=1
  for KEY in $KEYS
  do
      if echo $KEY | grep -q '#'; then
          keyserver=`echo $KEY | cut -d# -f1`
          keyids=`echo $KEY | cut -d# -f2 | tr ',' ' '`
          echo I: Downloading gpg public key: $keyserver $keyids
          flock /root/.gnupg.molior gpg --no-default-keyring --keyring=trustedkeys.gpg --keyserver $keyserver --recv-keys $keyids
          gpg --no-default-keyring --keyring=trustedkeys.gpg --export --armor $keyids > "$keydir/$i.asc"
      else
          echo I: Downloading gpg public key: $KEY
          keyfile="$keydir/$i.asc"
          wget -q $KEY -O $keyfile
          cat $keyfile | flock /root/.gnupg.molior gpg --import --no-default-keyring --keyring=trustedkeys.gpg
      fi
      i=$((i + 1))
  done

  rm -rf $target/
  echo I: Debootstrapping $DIST_RELEASE/$ARCH from $REPO_URL
  if [ "$ARCH" = "armhf" -o "$ARCH" = "arm64" ]; then
    debootstrap --foreign --arch $ARCH --variant=buildd --keyring=/root/.gnupg/trustedkeys.gpg $INCLUDE $COMPONENTS $DIST_RELEASE $target $REPO_URL
    if [ "$ARCH" = "armhf" ]; then
      cp /usr/bin/qemu-arm-static $target/usr/bin/
    else
      cp /usr/bin/qemu-aarch64-static $target/usr/bin/
    fi
    chroot $target /debootstrap/debootstrap --second-stage --no-check-gpg
  else
    debootstrap --variant=buildd --arch $ARCH --keyring=/root/.gnupg/trustedkeys.gpg $INCLUDE $COMPONENTS $DIST_RELEASE $target $REPO_URL
  fi

  echo I: Configuring chroot
  echo 'APT::Install-Recommends "false";' >$target/etc/apt/apt.conf.d/77molior
  echo 'APT::Install-Suggests "false";'  >>$target/etc/apt/apt.conf.d/77molior
  echo 'APT::Acquire::Retries "3";'      >>$target/etc/apt/apt.conf.d/77molior
  echo 'Acquire::Languages "none";'      >>$target/etc/apt/apt.conf.d/77molior

  # Disable debconf questions so that automated builds won't prompt
  echo set debconf/frontend Noninteractive | chroot $target debconf-communicate
  echo set debconf/priority critical | chroot $target debconf-communicate

  # Disable daemons in chroot:
  cat >> $target/usr/sbin/policy-rc.d <<EOM
#!/bin/sh
while true; do
    case "\$1" in
      -*) shift ;;
      makedev) exit 0;;
      x11-common) exit 0;;
      *) exit 101;;
    esac
done
EOM
  chmod +x $target/usr/sbin/policy-rc.d

  # Set up expected /dev entries
  if [ ! -r $target/dev/stdin ];  then ln -s /proc/self/fd/0 $target/dev/stdin;  fi
  if [ ! -r $target/dev/stdout ]; then ln -s /proc/self/fd/1 $target/dev/stdout; fi
  if [ ! -r $target/dev/stderr ]; then ln -s /proc/self/fd/2 $target/dev/stderr; fi

  echo I: Adding gpg public keys to chroot
  for keyfile in $keydir/*
  do
    name=`basename $keyfile .asc`
    mv $keyfile $keydir/$name
    gpg --dearmour $keydir/$name
    mv $keydir/$name.gpg $target//etc/apt/trusted.gpg.d/
  done
  rm -rf $keydir

#  echo I: Adding gpg public keys to chroot
#  for keyfile in $keydir/*
#  do
#    cat $keyfile | chroot $target apt-key add - >/dev/null || true
#  done
#  rm -rf $keydir

  # Add Molior Source signing key
  # su molior -c "gpg1 --export --armor $DEBSIGN_GPG_EMAIL" | chroot $target gpg1 --import --no-default-keyring --keyring=trustedkeys.gpg
  # su molior -c "gpg1 --export --armor $DEBSIGN_GPG_EMAIL" | chroot $target apt-key add -

  echo I: Installing build environment
  chroot $target apt-get update
  chroot $target apt-get -y --force-yes install build-essential fakeroot eatmydata libfile-fcntllock-perl aptitude lintian
  chroot $target apt-get clean

  rm -f $target/var/lib/apt/lists/*Packages* $target/var/lib/apt/lists/*Release*

  echo I: Creating schroot config
  mkdir -p $CHROOT_D
  cat > $CHROOT_D/sbuild-$CHROOT_NAME <<EOM
[$CHROOT_NAME]
description=Molior $CHROOT_NAME schroot
type=directory
directory=$target
groups=sbuild
root-groups=sbuild
profile=sbuild
command-prefix=eatmydata
EOM

  echo I: schroot $target created
}

publish_chroot()
{
  rm -f $target.tar.xz

  echo I: Creating schroot tar
  cd $target
  XZ_OPT="--threads=`nproc --ignore=1`" tar -cJf ../$CHROOT_NAME.tar.xz .
  cd - > /dev/null
  rm -rf $target

  echo I: schroot $target is ready
}

case "$ACTION" in
  info)
    echo "schroot build environment"
    ;;
  build)
    build_chroot
    ;;
  publish)
    publish_chroot
    ;;
  remove)
    rm -f $CHROOT_D/sbuild-$CHROOT_NAME
    rm -rf $target $target.tar.xz
    ;;
  *)
    echo "Unknown action $ACTION"
    exit 1
    ;;
esac

