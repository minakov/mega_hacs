#!/bin/bash
git clone https://github.com/minakov/mega_hacs.git
mkdir -p custom_components
cp -r mega_hacs/custom_components/mega custom_components/mega
rm -fR mega_hacs
