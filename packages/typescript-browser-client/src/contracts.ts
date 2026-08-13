/** Runtime validation for generated public contract models. */

import {
  contractSchemas,
  type ContractSchemaName,
} from "./generated-models.js";

type Schema = Readonly<Record<string, unknown>>;

/** A value does not match its selected public contract schema. */
export class ContractValidationError extends Error {}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function schemaRecord(value: unknown): Schema {
  if (!isRecord(value)) {
    throw new ContractValidationError(
      "The generated schema node is not an object.",
    );
  }
  return value;
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function checkType(type: unknown, value: unknown): boolean {
  if (Array.isArray(type)) {
    return type.some((item) => checkType(item, value));
  }
  switch (type) {
    case undefined:
      return true;
    case "null":
      return value === null;
    case "object":
      return isRecord(value);
    case "array":
      return Array.isArray(value);
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    case "string":
      return typeof value === "string";
    default:
      return false;
  }
}

function localReference(reference: string): Schema {
  const prefix = "#/components/schemas/";
  if (!reference.startsWith(prefix)) {
    throw new ContractValidationError(
      `Unsupported contract reference: ${reference}`,
    );
  }
  const name = reference.slice(prefix.length) as ContractSchemaName;
  const schema: unknown = contractSchemas[name];
  if (schema === undefined) {
    throw new ContractValidationError(
      `Unknown contract reference: ${reference}`,
    );
  }
  return schemaRecord(schema);
}

function compositionMatches(
  schema: Schema,
  value: unknown,
  path: string,
): boolean {
  try {
    assertSchema(schema, value, path);
    return true;
  } catch (error: unknown) {
    if (error instanceof ContractValidationError) {
      return false;
    }
    throw error;
  }
}

function assertComposition(schema: Schema, value: unknown, path: string): void {
  for (const key of ["allOf", "anyOf", "oneOf"] as const) {
    const alternatives = schema[key];
    if (!Array.isArray(alternatives)) {
      continue;
    }
    const matches = alternatives.filter((alternative) =>
      compositionMatches(schemaRecord(alternative), value, path),
    ).length;
    const valid =
      key === "allOf"
        ? matches === alternatives.length
        : key === "oneOf"
          ? matches === 1
          : matches > 0;
    if (!valid) {
      throw new ContractValidationError(`${path} does not match ${key}.`);
    }
  }
  if (isRecord(schema.not) && compositionMatches(schema.not, value, path)) {
    throw new ContractValidationError(`${path} matches a forbidden shape.`);
  }
}

function assertString(schema: Schema, value: string, path: string): void {
  const minLength = schema.minLength;
  const maxLength = schema.maxLength;
  if (typeof minLength === "number" && value.length < minLength) {
    throw new ContractValidationError(`${path} is shorter than minLength.`);
  }
  if (typeof maxLength === "number" && value.length > maxLength) {
    throw new ContractValidationError(`${path} is longer than maxLength.`);
  }
  const pattern = schema.pattern;
  if (typeof pattern === "string" && !new RegExp(pattern, "u").test(value)) {
    throw new ContractValidationError(`${path} does not match its pattern.`);
  }
  const format = schema.format;
  const validFormat =
    format === undefined ||
    (format === "date-time" &&
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u.test(
        value,
      ) &&
      !Number.isNaN(Date.parse(value))) ||
    (format === "uuid" &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(
        value,
      )) ||
    (format === "uri" && URL.canParse(value));
  if (!validFormat) {
    throw new ContractValidationError(`${path} does not match its format.`);
  }
}

function assertNumber(schema: Schema, value: number, path: string): void {
  const minimum = schema.minimum;
  const maximum = schema.maximum;
  if (typeof minimum === "number" && value < minimum) {
    throw new ContractValidationError(`${path} is less than minimum.`);
  }
  if (typeof maximum === "number" && value > maximum) {
    throw new ContractValidationError(`${path} is greater than maximum.`);
  }
}

function assertArray(schema: Schema, value: unknown[], path: string): void {
  const minItems = schema.minItems;
  const maxItems = schema.maxItems;
  if (typeof minItems === "number" && value.length < minItems) {
    throw new ContractValidationError(`${path} has fewer than minItems.`);
  }
  if (typeof maxItems === "number" && value.length > maxItems) {
    throw new ContractValidationError(`${path} has more than maxItems.`);
  }
  if (schema.uniqueItems === true) {
    const items = value.map((item) => JSON.stringify(item));
    if (new Set(items).size !== items.length) {
      throw new ContractValidationError(`${path} has duplicate items.`);
    }
  }
  const items = schema.items;
  if (isRecord(items)) {
    value.forEach((item, index) => {
      assertSchema(items, item, `${path}[${String(index)}]`);
    });
  }
}

function assertObject(
  schema: Schema,
  value: Record<string, unknown>,
  path: string,
): void {
  const properties = isRecord(schema.properties) ? schema.properties : {};
  const required = Array.isArray(schema.required)
    ? schema.required.filter((item): item is string => typeof item === "string")
    : [];
  for (const name of required) {
    if (!(name in value)) {
      throw new ContractValidationError(`${path}.${name} is required.`);
    }
  }
  for (const [name, item] of Object.entries(value)) {
    const propertySchema = properties[name];
    if (propertySchema !== undefined) {
      assertSchema(schemaRecord(propertySchema), item, `${path}.${name}`);
    } else if (schema.additionalProperties === false) {
      throw new ContractValidationError(`${path}.${name} is not allowed.`);
    } else if (isRecord(schema.additionalProperties)) {
      assertSchema(schema.additionalProperties, item, `${path}.${name}`);
    }
  }
}

function assertSchema(schema: Schema, value: unknown, path: string): void {
  const reference = schema.$ref;
  if (typeof reference === "string") {
    assertSchema(localReference(reference), value, path);
    return;
  }
  if ("const" in schema && !sameJson(schema.const, value)) {
    throw new ContractValidationError(
      `${path} does not equal its constant value.`,
    );
  }
  const enumValues = schema.enum;
  if (
    Array.isArray(enumValues) &&
    !enumValues.some((candidate) => sameJson(candidate, value))
  ) {
    throw new ContractValidationError(`${path} is not an accepted enum value.`);
  }
  assertComposition(schema, value, path);
  if (!checkType(schema.type, value)) {
    throw new ContractValidationError(`${path} has the wrong type.`);
  }
  if (typeof value === "string") {
    assertString(schema, value, path);
  } else if (typeof value === "number") {
    assertNumber(schema, value, path);
  } else if (Array.isArray(value)) {
    assertArray(schema, value, path);
  } else if (isRecord(value)) {
    assertObject(schema, value, path);
  }
}

/** Validate and return one value against a closed generated schema. */
export function validateContract<T>(
  schemaName: ContractSchemaName,
  value: T,
): T {
  assertSchema(schemaRecord(contractSchemas[schemaName]), value, schemaName);
  return value;
}
