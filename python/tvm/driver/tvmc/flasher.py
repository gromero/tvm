# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Provides support to flash µTVM compiled runtimes.
"""
import os
import shutil

from tvm.micro.micro_binary import MicroBinary
from tvm.micro.contrib.zephyr import ZephyrFlasher

from . import common, frontends
from .main import register_parser


@register_parser
def add_flasher_parser(subparsers):
    """ Include parser for 'flash' subcommand """

    parser = subparsers.add_parser("flash", help="flash a model")
    parser.set_defaults(func=drive_flash)
    parser.add_argument(
        "--runtime",
        default="./runtime.tar",
        metavar="TARFILE",
        help="path to the runtime.tar file with the image to be flashed",
    )

def drive_flash(args):
    flash_model(args.runtime)

    return 0

def flash_model(runtime_tar):
    temp_dir = "/tmp/tvmc_tmp"
    if os.path.exists(temp_dir):
        print(f"Cleaning temporary dir ({temp_dir})...")
        shutil.rmtree(temp_dir)

    print(f"Flashing runtime from '{runtime_tar}'...")
    micro_binary = MicroBinary.unarchive(archive_path=runtime_tar, base_dir=temp_dir)
    flasher = ZephyrFlasher(west_cmd=["west"], subprocess_env={})
    flasher.flash(micro_binary)

    shutil.rmtree(temp_dir)
