import type { Model, ObservedRequirement } from "./api.ts";

type CapabilityModel = Pick<
  Model,
  "input_modalities" | "output_modalities" | "capabilities"
>;

export const configurationCapabilities = {
  text_input: { label: "Text", direction: "input", tone: "blue" },
  image_input: { label: "Image", direction: "input", tone: "violet" },
  text_output: { label: "Text", direction: "output", tone: "blue" },
  embedding_output: { label: "Embeddings", direction: "output", tone: "amber" },
  image_output: { label: "Image", direction: "output", tone: "violet" },
  video_output: { label: "Video", direction: "output", tone: "coral" },
  audio_output: { label: "Audio", direction: "output", tone: "teal" },
} as const;

export type ConfigurationCapability = keyof typeof configurationCapabilities;

export function modelHasCapability(
  model: CapabilityModel,
  capability: ObservedRequirement,
): boolean {
  if (capability.endsWith("_input"))
    return model.input_modalities.some(
      (value) => `${value}_input` === capability,
    );
  if (capability.endsWith("_output"))
    return model.output_modalities.some(
      (value) => `${value}_output` === capability,
    );
  return model.capabilities.some((value) => value === capability);
}

export function modelCapabilityKeys(
  model: CapabilityModel,
): readonly ConfigurationCapability[] {
  return (
    Object.keys(configurationCapabilities) as ConfigurationCapability[]
  ).filter((capability) => modelHasCapability(model, capability));
}
