const CAMERA_RAW_PARAM_NAMES = new Set([
  "temperature",
  "tint",
  "exposure",
  "contrast",
  "highlights",
  "shadows",
  "whites",
  "blacks",
  "texture",
  "clarity",
  "dehaze",
  "vibrance",
  "saturation",
]);

// Replace this object only after a descriptor has been recorded in a licensed
// Photoshop host, sanitized, reviewed, hash-pinned by the Python protocol, and
// committed with the same fixture_id. Callers can never provide descriptors.
const REVIEWED_CAMERA_RAW_FIXTURE = Object.freeze({
  verified: false,
  fixture_id: "",
  descriptors: Object.freeze([]),
});

function renderTemplate(value, params) {
  if (Array.isArray(value)) {
    return value.map((item) => renderTemplate(item, params));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, renderTemplate(item, params)]),
    );
  }
  if (typeof value === "string" && value.startsWith("{{") && value.endsWith("}}")) {
    const variable = value.slice(2, -2).trim();
    if (!variable.startsWith("params.")) throw new Error("camera_raw_template_forbidden");
    const name = variable.slice("params.".length);
    if (!CAMERA_RAW_PARAM_NAMES.has(name)) throw new Error("camera_raw_parameter_forbidden");
    return params[name];
  }
  return value;
}

export function cameraRawFixtureStatus() {
  return {
    verified: REVIEWED_CAMERA_RAW_FIXTURE.verified === true,
    fixture_id: REVIEWED_CAMERA_RAW_FIXTURE.fixture_id,
    descriptor_count: REVIEWED_CAMERA_RAW_FIXTURE.descriptors.length,
  };
}

export function compileReviewedCameraRawDescriptors(fixtureId, params) {
  const status = cameraRawFixtureStatus();
  if (
    !status.verified ||
    !status.fixture_id ||
    status.fixture_id !== String(fixtureId || "") ||
    status.descriptor_count < 1
  ) {
    throw new Error("camera_raw_reviewed_fixture_unavailable");
  }
  return REVIEWED_CAMERA_RAW_FIXTURE.descriptors.map((descriptor) =>
    renderTemplate(descriptor, params),
  );
}
