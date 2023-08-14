Electrum-Evrmore lightweight client
===================================

  Licence: MIT Licence
  Author: Thomas Voegtlin
  Port to Ravencoin: Kralverde
  Port to Evrmore: Hans Schmidt
  Language: Python (>= 3.8)
  

Getting started
===============

Contact us on Discord at https://discord.gg/4csauGuvw3

Note: Hardware wallet support for Evrmore has not yet been tested or debugged
    -Want to use Ledger? Make sure ledger live is closed.
    -Want to mine to hardware? Large transactions are likely to cause problems. It is recommended that you set up a software wallet and mine to that. 
        Then send the EVR to your hardware wallet from the software wallet.

Interested in a pre-built binary? They have been built and posted for Linux and Windows

Electrum Evrmore is currently only supported for Qt-based software.

The master branch is not always guaranteed to be working as expected. If you would like to build from source or run raw python, please use the source from one of our releases.

Learn how to run your own ElectrumX Server.

   
Not pure-python dependencies
----------------------------

If you want to use the Qt interface, install the Qt dependencies::

    sudo apt-get install python3-pyqt5

For elliptic curve operations, "libsecp256k1" is a required dependency::

    sudo apt-get install libsecp256k1-0

Alternatively, when running from a cloned repository, a script is provided to build
libsecp256k1 yourself::

    sudo apt-get install automake libtool
    ./contrib/make_libsecp256k1.sh

Due to the need for fast symmetric ciphers, "cryptography"_ is required.
Install from your package manager (or from pip)::

    sudo apt-get install python3-cryptography

Electrum-Evrmore require the  "evrhash" module, which is not yet available on PyPi. So will need to build and install it.

libsecp256k1: https://github.com/bitcoin-core/secp256k1
pycryptodomex: https://github.com/Legrandin/pycryptodome
cryptography: https://github.com/pyca/cryptography
this: https://github.com/spesmilo/electrum-docs/blob/master/hardware-linux.rst
here: https://github.com/EvrmoreOrg/electrum-evrmore/releases
Discord: https://discord.gg/4csauGuvw3
article: https://support.ledger.com/hc/en-us/articles/360018969814-Receive-mining-proceeds?docs=true
releases: https://github.com/EvrmoreOrg/electrum-evrmore/releases
ElectrumX Server: https://github.com/EvrmoreOrg/electrumx-evrmore


Running from tar.gz
-------------------

Electrum for bitcoin provides a tar.gz version which contains all the python dependencies in a "packages" subdirectory,
which makes it easy to run Electrum without any building or installation, using only the command "./run_electrum". 
The non-python dependencies still must previously have been installed on the system.

A tar.gz is not provided for Electrum-Evrmore because it would not be very helpful. Since Electrum-Evrmore requires the "evrhash" module, 
    which is not yet available on PyPi, it must be built and loaded on the system in advance.


Development version (git clone)
-------------------------------

Electrum itself is pure Python, and so are most of the required dependencies, but not everything. 
To run from source:

Install python3
	sudo apt-get install python3

Make sure Python 3.8.5 or higher is installed by typing:
	python3 --version

Install the Electrum non-Pyhon dependencies:
    sudo apt-get install python3
    sudo apt-get install python3-pip
    sudo apt-get install python3-cryptography
    sudo apt-get install python3-pyqt5
    sudo apt-get install libsecp256k1-0
    sudo apt-get install cmake
    sudo pip3 install virtualenv
	sudo pip install --upgrade protobuf==3.19	# without this, some Linux version will crash at runtime with error: 
												#		"AttributeError: module 'google.protobuf.descriptor' has no attribute '_internal_create_key'"

For Ubuntu, we need to install venv. Most other platforms have it pre-installed as part of python:
	sudo apt-get install python3.8-venv

Check out the code from GitHub::
    cd
    git clone git://github.com/EvrmoreOrg/electrum-evrmore.git
	cd electrum-evrmore/

Note that the "electrum-env" script which is used to launch Electrum-Evrmore assumes the existence of a python venv called "env", so it must be built in advance if you
    want anything special in it. Since Electrum-Evrmore requires the "evrhash" module, which is not yet available on PyPi, and since you probably want to avoid modifying the 
    system-wide python "site-packages", you should build and load it in the local "env" venv.

After building evrhash in the "env" (and with "env" still active), install electrum-evrmore:
	cd ~/ electrum-evrmore/
	pip install -e .

Nown deactivate "env" and launch Electrum-Evrmore with the command:
    deactivate
    ./electrum-env or ./electrum-env --testnet


Creating Binaries
=================

Linux (tarball)
---------------

See contrib/build-linux/sdist/README.md


Linux (AppImage)
----------------

See contrib/build-linux/appimage/README.md


Mac OS X / macOS
----------------

See contrib/osx/README.md


Windows
-------

See contrib/build-wine/README.md


Android
-------

See contrib/android/Readme.md


Contributing
============

Any help testing the software, reporting or fixing bugs, reviewing pull requests
and recent changes, writing tests, or helping with outstanding issues is very welcome.
Implementing new features, or improving/refactoring the codebase, is of course
also welcome, but to avoid wasted effort, especially for larger changes,
we encourage discussing these on the discord first.

Besides GitHub, most communication about Electrum development happens on Discord, in the
#development channel. Join us at https://discord.gg/4csauGuvw3

GitHub: https://github.com/EvrmoreOrg/electrum-evrmore
