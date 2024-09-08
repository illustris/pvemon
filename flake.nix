rec {
	description = "PVE prometheus exporter that collects metrics locally rather than use the PVE API";

	inputs = {
		nixpkgs.url = github:nixos/nixpkgs/nixos-unstable;
		debBundler = {
			url = github:illustris/flake;
			flake = false;
		};
	};

	outputs = { self, nixpkgs, debBundler }: {

		packages.x86_64-linux = with nixpkgs.legacyPackages.x86_64-linux; rec {
			pvemon = python3Packages.buildPythonApplication {
				pname = "pvemon";
				version = "1.2.0";
				src = ./src;
				propagatedBuildInputs = with python3Packages; [
					pexpect
					prometheus-client
					psutil
				];

				meta = {
					inherit description;
					license = lib.licenses.mit;
				};
			};
			default = pvemon;
			deb = (import "${debBundler}/bundlers/deb" { inherit pkgs; }) default;
			updateRelease = writeScriptBin "update-release" (builtins.readFile ./utils/update-release.sh);
		};

	};
}
