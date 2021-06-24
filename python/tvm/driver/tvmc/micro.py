from .main import register_parser

@register_parser
def add_micro_parser(subparsers):
    micro = subparsers.add_parser("micro", help="select micro context.")
    micro.set_defaults(func=drive_micro)

    micro_parser = micro.add_subparsers(title="subcommands")

    # 'create_project' subcommand
    create_project_parser = micro_parser.add_parser("create_project", help="create a project template of a given type.")
    create_project_parser.set_defaults(subcommand=create_project_handler)
    create_project_parser.add_argument("--type", required=True, help="type of the project to create a template. e.g Zephyr.")
    create_project_parser.add_argument("--dir", help="output directory where the project template will be created.")

    # 'build' subcommand
    build_parser = micro_parser.add_parser("build", help="build an image based on a project dir.")
    build_parser.set_defaults(subcommand=build_handler)
    build_parser.add_argument('--target', required=True)
    build_parser.add_argument('--board', required=True)

    # 'flash' subcommand
    flash_parser = micro_parser.add_parser("flash", help="flash the built image on a given micro target.")
    flash_parser.set_defaults(subcommand=flash_handler)

    # 'run' subcommand
    run_parser = micro_parser.add_parser("run", help="run a flashed image (with a model).")
    run_parser.set_defaults(subcommand=run_handler)

def drive_micro(args):
#   print(f"{args.func}\n{args.subcommand}")
    # Call proper handler based on what parser found
    args.subcommand(args)

def create_project_handler(args):
    print("Calling create_project handler...")

def build_handler(args):
    print("Calling build handler...")

def flash_handler(args):
    print("Calling flash handler...")

def run_handler(args):
    print("Calling run handler...")
