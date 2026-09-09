class Cgate < Formula
  desc "Middleware/CLI between AI agents and servers — IA proposes, human approves."
  homepage "https://github.com/wanderlp/command-gate"
  url "https://github.com/wanderlp/command-gate/releases/download/v0.1.5/cgate-macos-arm64"
  version "0.1.5"
  sha256 "PLACEHOLDER_UPDATE_AFTER_RELEASE"

  on_intel do
    url "https://github.com/wanderlp/command-gate/releases/download/v0.1.5/cgate-macos-x86_64"
  end

  def install
    bin.install "cgate-macos-arm64" => "cgate"
  end

  test do
    assert_match "cgate #{version}", shell_output("#{bin}/cgate --version")
  end
end
