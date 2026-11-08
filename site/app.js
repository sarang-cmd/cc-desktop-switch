(() => {
  const repo = "sarang-cmd/junction";
  const releaseUrl = `https://github.com/${repo}/releases/latest`;
  const apiUrl = `https://api.github.com/repos/${repo}/releases/latest`;
  const patterns = {
    "windows-installer": /windows.*(?:setup|installer).*\.exe$/i,
    "windows-portable": /windows.*portable.*\.zip$/i,
    "mac-arm-pkg": /macos-arm64\.pkg$/i,
    "mac-arm-dmg": /macos-arm64\.dmg$/i,
    "mac-intel-pkg": /macos-x64\.pkg$/i,
    "mac-intel-dmg": /macos-x64\.dmg$/i,
  };
  const setReleaseLink = (selector, url, name) => {
    document.querySelectorAll(`[data-asset="${selector}"]`).forEach((link) => {
      link.href = url || releaseUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      if (name) link.setAttribute("aria-label", `${link.textContent.trim()}: ${name}`);
    });
  };

  document.querySelectorAll("[data-asset]").forEach((link) => {
    link.href = releaseUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
  const heroDownload = document.querySelector("#hero-download");
  if (heroDownload) heroDownload.href = releaseUrl;

  fetch(apiUrl, { headers: { Accept: "application/vnd.github+json" } })
    .then((response) => {
      if (!response.ok) throw new Error(`Release lookup returned ${response.status}`);
      return response.json();
    })
    .then((release) => {
      const assets = Array.isArray(release.assets) ? release.assets : [];
      const version = String(release.tag_name || "").replace(/^v/i, "");
      const versionText = version ? `Latest release · v${version}` : "Latest release";
      const versionNode = document.querySelector("#download-version");
      const noteNode = document.querySelector("#release-note");
      if (versionNode) versionNode.textContent = versionText;
      if (noteNode) noteNode.textContent = version ? `Windows and macOS · Version ${version}` : "Windows and macOS · Latest release";
      const releaseLink = release.html_url || releaseUrl;
      const heroLabel = heroDownload?.querySelector(".button-label");
      if (heroLabel) heroLabel.textContent = version ? `Get Junction for Windows · v${version}` : "Get Junction for Windows";
      if (heroDownload) heroDownload.href = releaseLink;

      Object.entries(patterns).forEach(([key, pattern]) => {
        const asset = assets.find((candidate) => pattern.test(String(candidate.name || "")));
        setReleaseLink(key, asset?.browser_download_url || releaseLink, asset?.name);
      });
    })
    .catch(() => {
      const versionNode = document.querySelector("#download-version");
      const noteNode = document.querySelector("#release-note");
      if (versionNode) versionNode.textContent = "Latest release on GitHub";
      if (noteNode) noteNode.textContent = "Windows and macOS · Releases and checksums on GitHub";
    });
})();
