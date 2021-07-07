# tvmc micro create-project -l                                                   # list available templates
# tvmc micro create-project -t [zephyr,arduino,mbed,...] PROJECT_DIR MLF_ARCHIVE # select template by type
# tvmc micro create-project TEMPLATE_DIR PROJECT_DIR MLF_ARCHIVE [-d] [-t]       # -d enabled verbose when building
# tvmc micro create-project -t zephyr /tmp/x10 --board [BOARD]                   # --board

# tvmc micro build PROJECT_DIR options: list   # build project with options
# tvmc micro build PROJECT_DIR --board -

# tvmc micro flash -l                          # list serial ports
# tvmc micro flash -p [SERIAL_PORT]            # flash built image to the device

# tvmc run PROJECT_DIR -p [SERIAL_PORT]        # run flashed model on device attached to SERIAL_PORT

# tvmc micro tunning ?

import os
import shutil
from pathlib import Path

import tvm.micro.project as project
from .main import register_parser
from .common import TVMCException


ZEPHYR_TEMPLATE_DIR = os.getenv("TVM_HOME") + "/apps/microtvm/zephyr/template_project"


TEMPLATE_TYPE = {'zephyr': ZEPHYR_TEMPLATE_DIR,}


def template_types():
    types = [ t for t in TEMPLATE_TYPE ]
    types = ", ".join(types)
    return types


@register_parser
def add_micro_parser(subparsers):
    micro = subparsers.add_parser("micro", help="select micro context.")
    micro.set_defaults(func=drive_micro)

    micro_parser = micro.add_subparsers(title="subcommands")

    # 'create_project' subcommand
    create_project_parser = micro_parser.add_parser("create-project", help="create a project template of a given type or given a template dir.")
    create_project_parser.set_defaults(subcommand=create_project_handler)
    create_project_parser.add_argument("TEMPLATE_DIR", nargs='?', help="Project template directory to be used to create a new project.")
    create_project_parser.add_argument("-t", "--template-type", choices=TEMPLATE_TYPE.keys(),
        help=f"Specify a template type instead of a template dir to create a new project dir. Available types: {template_types()}.")
    create_project_parser.add_argument("PROJECT_DIR", help="Project dir where the new project based on the template dir will be create.")
    create_project_parser.add_argument("MLF", help="MLF .tar archive.")
    create_project_parser.add_argument("--board", required=True, help="Target board.")
    create_project_parser.add_argument("-f","--force", action="store_true", help="Force project creating even if the specified PROJECT_DIR already exists.")
    create_project_parser.add_argument("-V","--verbose", action="store_true", help="FIXME: Enable verbosity when building the new project.")

    # 'build' subcommand
    build_parser = micro_parser.add_parser("build", help="build a project dir, generally creating an image to be flashed, e.g. zephyr.elf.")
    build_parser.set_defaults(subcommand=build_handler)
    build_parser.add_argument("PROJECT_DIR", help="Project dir to build.")
    build_parser.add_argument("--board", required=True, help="Target board.")
    build_parser.add_argument("-f","--force", action="store_true", help="Force rebuild.")
    build_parser.add_argument("-V","--verbose", action="store_true", help="Enable verbosity when building the project.")

    # 'flash' subcommand
    flash_parser = micro_parser.add_parser("flash", help="flash the built image on a given micro target.")
    flash_parser.set_defaults(subcommand=flash_handler)
    flash_parser.add_argument("PROJECT_DIR", help="Project dir with a image built.")
    flash_parser.add_argument("--board", required=True, help="Target board.")
    flash_parser.add_argument("-B","--build", action="store_true", help="Build image if one is not found in the project dir.")
    flash_parser.add_argument("-V","--verbose", action="store_true", help="Enable verbosity when building the project.")
    # TODO(gromero): list and select serial when multiple devices exist.

    # 'run' subcommand
    run_parser = micro_parser.add_parser("run", help="run a flashed image (with a model).")
    run_parser.set_defaults(subcommand=run_handler)


def drive_micro(args):
#   print(f"{args.func}\n{args.subcommand}")
    # Call proper handler based on what parser found
    args.subcommand(args)


def create_project_handler(args):
    print("Calling create_project handler...")

    # print(args.verbose)
    # print(args.TEMPLATE_DIR)
    # print(args.template_type)

    if args.TEMPLATE_DIR and args.template_type:
        raise TVMCException("A template dir and a template type can't be both specified at the same time.")

    if not args.TEMPLATE_DIR and not args.template_type:
        raise TVMCException("Either a template dir or a template type must be specified!")

    if args.TEMPLATE_DIR:
        template_dir = args.TEMPLATE_DIR

    if args.template_type:
        template_dir = TEMPLATE_TYPE[args.template_type]

    if os.path.exists(args.PROJECT_DIR):
        if args.force:
            shutil.rmtree(args.PROJECT_DIR)
        else:
            raise TVMCException("The specified project dir already exists. To force overwriting it use '-f' or '--force'.")

    project_dir = args.PROJECT_DIR

    mlf_path = str(Path(args.MLF).resolve())

    # TODO(gromero): add arg to set 'west_cmd' too?
    options = {
        'zephyr_board': args.board,
        'west_cmd': 'west',
        'verbose': args.verbose,
    }

    project.generate_project(template_dir, project_dir, mlf_path=mlf_path, options=options)


def build_handler(args):
    print("Calling build handler...")

    if not os.path.exists(args.PROJECT_DIR):
        raise TVMCException(f"{args.PROJECT_DIR} doesn't exist.")

    if os.path.exists(args.PROJECT_DIR + "/build"):
        if args.force:
            shutil.rmtree(args.PROJECT_DIR + "/build")
        else:
            raise TVMCException(f"There is already a build in {args.PROJECT_DIR}. To force rebuild it use '-f' or '--force'.")

    project_dir = args.PROJECT_DIR

    # TODO(gromero): add arg to set 'west_cmd' too?
    options = {
        'zephyr_board': args.board,
        'west_cmd': 'west',
        'verbose': args.verbose,
    }

    _project = project.GeneratedProject.from_directory(project_dir, options=options)
    _project.build()


def flash_handler(args):
    print("Calling flash handler...")

    if not os.path.exists(args.PROJECT_DIR + "/build"):
        raise TVMCException(f"Could not find a build in {args.PROJECT_DIR}")

    project_dir = args.PROJECT_DIR

    options = {
        'zephyr_board': args.board,
        'west_cmd': 'west',
        'verbose': args.verbose,
    }

    _project = project.GeneratedProject.from_directory(project_dir, options=options)
    _project.flash()

def run_handler(args):
    print("Calling run handler...")
