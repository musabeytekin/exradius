import { encode, matching, pieces, typing } from "./mentions";

test("bodies render mentions with today's names", () => {
  expect(pieces("hi @[person:2], ask @[exradius]!", { "2": "Ben Okafor" })).toEqual([
    { text: "hi " }, { mention: "Ben Okafor", exradius: false }, { text: ", ask " },
    { mention: "Exradius", exradius: true }, { text: "!" },
  ]);
  expect(pieces("@[person:9]", {})).toEqual([{ mention: "someone", exradius: false }]);
});

test("the mention being typed", () => {
  expect(typing("ask @Be", 7)).toEqual({ start: 4, query: "Be" });
  expect(typing("@", 1)).toEqual({ start: 0, query: "" });
  expect(typing("mail a@b", 8)).toBeNull();
  expect(typing("ask @Ben now", 12)).toBeNull();
});

test("candidates match any word of the name", () => {
  const people = [{ token: "@[person:1]", name: "Ada Lovelace" }, { token: "@[person:2]", name: "Ben Okafor" }];
  expect(matching(people, "lov").map((c) => c.name)).toEqual(["Ada Lovelace"]);
  expect(matching(people, "").length).toBe(2);
});

test("chosen names become references, longest first", () => {
  const chosen = [
    { token: "@[person:1]", name: "Ada" },
    { token: "@[person:3]", name: "Ada Lovelace" },
    { token: "@[exradius]", name: "Exradius", exradius: true },
  ];
  expect(encode("@Exradius ask @Ada Lovelace and @Ada, not @Adam", chosen))
    .toBe("@[exradius] ask @[person:3] and @[person:1], not @Adam");
  expect(encode("@Ben", [])).toBe("@Ben");
});
