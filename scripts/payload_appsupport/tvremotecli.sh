#!/bin/bash
# Requires homebrew https://brew.sh
# /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

print_usage() {
    cat <<'EOF'
Usage:
  tvcontrol --volume [OPTIONS] {up,down,mute}
  tvcontrol --media [OPTIONS] {play-pause,play,pause,stop,next,prev,rewind,ff,record}
  tvcontrol --key [OPTIONS] {POWER,HOME,BACK,SEARCH,MENU,SETTINGS}
  tvcontrol --dpad [OPTIONS] {up,down,left,right,center,a,b,x,y}
  tvcontrol --app [OPTIONS]
  tvcontrol --text [OPTIONS] {text}
  tvcontrol --power
  tvcontrol --input [OPTIONS] {hdmi1, hdmi2, hdmi3}
  
Examples:
  tvcontrol --volume up
  tvcontrol --key HOME
  tvcontrol --dpad left
  tvcontrol --input hdmi1

EOF
}

if [[ $# -eq 0 ]]; then
    print_usage
    exit 0
fi

init_venv() {
	python3 -m venv .venv
	source .venv/bin/activate
	
	# Install dependencies
	python -m pip install --upgrade pip
	brew install portaudio
	pip install pyaudio
	python -m pip install -e .
	
	# Generate *_pb2.py from *.proto
	python -m pip install grpcio-tools mypy-protobuf
	python -m grpc_tools.protoc src/androidtvremote2/*.proto --python_out=src/androidtvremote2 --mypy_out=src/androidtvremote2 -Isrc/androidtvremote2
	
	# Run pre-commit
	python -m pip install pre-commit
	pre-commit install
	pre-commit run --all-files
	
	# Run tests
	python -m pip install -e ".[test]"
	pytest
	
	# Run demo
	python -m pip install -e ".[demo]"
	python src/demo.py
	
	# Build package
	python -m pip install build
	python -m build
}


if [ ! -d "$SCRIPT_DIR/.venv" ]; then
	    cd "$SCRIPT_DIR"
	    echo "Initialising Python virtual environment."
	    init_venv		    
fi

"$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/tvremotecli.py" $1 $2

