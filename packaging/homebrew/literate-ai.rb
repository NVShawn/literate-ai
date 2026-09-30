class LiterateAi < Formula
  include Language::Python::Virtualenv

  LITERATE_AI_VERSION = "1.1.0"

  desc "Specification-led software lifecycle control plane"
  homepage "https://github.com/NVIDIA-dev/literate-ai"
  license "Apache-2.0"
  version LITERATE_AI_VERSION

  # Replace url/sha256 with the published sdist when the matching tag exists.
  url "https://github.com/NVIDIA-dev/literate-ai/archive/refs/tags/v#{LITERATE_AI_VERSION}.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/litai --version")
  end
end
