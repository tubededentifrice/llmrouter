import { useState } from "react";
import {
  AdvancedFieldsDisclosure,
  FormGrid,
  FormSection,
  SelectControl,
  TextControl,
} from "@opendle/ui";
import type { Model, ProviderModel, ReasoningLevel } from "./api.ts";

const strategies = [
  ["auto", "Automatic"],
  ["none", "Do not send reasoning"],
  ["effort", "reasoning_effort"],
  ["nested_effort", "reasoning.effort (OpenRouter)"],
  ["thinking_type", "thinking.type (GLM)"],
  ["system_token", "System token (Gemma)"],
  ["native", "think (Ollama)"],
] as const;
const levels: readonly ReasoningLevel[] = ["none", "low", "medium", "high"];

export function ReasoningFields({
  model,
  mapping,
  route = false,
}: {
  readonly model?: Model | undefined;
  readonly mapping?: ProviderModel | undefined;
  readonly route?: boolean;
}) {
  const [strategy, setStrategy] = useState(
    route
      ? (mapping?.configured_reasoning_strategy ?? "")
      : (model?.reasoning_strategy ?? "auto"),
  );
  const [level, setLevel] = useState(
    route
      ? (mapping?.configured_default_reasoning_level ?? "")
      : (model?.default_reasoning_level ?? ""),
  );
  const [mappings, setMappings] = useState(() =>
    Object.fromEntries(
      levels.map((item) => [
        item,
        mapping?.reasoning_mappings.find((entry) => entry.level === item)
          ?.provider_value ?? "",
      ]),
    ),
  );
  return (
    <FormSection legend="Reasoning" variant="plain">
      <FormGrid>
        <SelectControl
          label="How to pass reasoning"
          name="reasoning_strategy"
          value={strategy}
          onChange={(event) => {
            setStrategy(event.currentTarget.value);
          }}
        >
          {route ? <option value="">Model default</option> : null}
          {strategies.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </SelectControl>
        <SelectControl
          label="Default reasoning level"
          name="default_reasoning_level"
          value={level}
          onChange={(event) => {
            setLevel(event.currentTarget.value);
          }}
        >
          <option value="">{route ? "Model default" : "Automatic"}</option>
          {levels.map((value) => (
            <option key={value}>{value}</option>
          ))}
        </SelectControl>
      </FormGrid>
      {route ? (
        <AdvancedFieldsDisclosure summary="Provider reasoning values">
          <FormGrid>
            {levels.map((value) => (
              <TextControl
                key={value}
                label={`${value[0]?.toUpperCase() ?? ""}${value.slice(1)} provider value`}
                name={`reasoning_mapping_${value}`}
                value={mappings[value] ?? ""}
                placeholder={value}
                onChange={(event) => {
                  const providerValue = event.currentTarget.value;
                  setMappings((current) => ({
                    ...current,
                    [value]: providerValue,
                  }));
                }}
              />
            ))}
          </FormGrid>
        </AdvancedFieldsDisclosure>
      ) : null}
    </FormSection>
  );
}
