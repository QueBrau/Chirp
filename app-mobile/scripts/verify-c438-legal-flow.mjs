/** Runtime state harness for the c438 acceptance and invite ownership rules. */
let failures = 0;
const pass = name => console.log(`PASS ${name}`);
const check = (name, value) => value ? pass(name) : (failures++, console.log(`FAIL ${name}`));

function inviteState(initial = "") {
  let code = initial;
  let edited = false;
  return {
    delayedParam(value) { if (!edited && value) code = value; },
    edit(value) { edited = true; code = value; },
    get code() { return code; },
  };
}

function policyState(ownerGeneration) {
  let generation = ownerGeneration;
  let age = 18;
  let guardian = false;
  let status = { required: true, material_change: false };
  let epoch = 0;
  return {
    switchAccount(nextGeneration) { generation = nextGeneration; epoch++; status = null; age = null; guardian = false; },
    loadResult(requestGeneration, requestEpoch, value) {
      if (requestGeneration === generation && requestEpoch === epoch) status = value;
    },
    submit(requestGeneration, requestEpoch) {
      return requestGeneration === generation && requestEpoch === epoch;
    },
    conflict() { status = null; age = null; guardian = false; },
    snapshot() { return { generation, epoch, status, age, guardian }; },
    request() { return { generation, epoch }; },
  };
}

const invite = inviteState();
invite.delayedParam("LATE-CODE");
check("delayed invite param prefills untouched form", invite.code === "LATE-CODE");
invite.edit("USER-CODE");
invite.delayedParam("STALE-CODE");
check("user edited invite survives later param", invite.code === "USER-CODE");

const flow = policyState(1);
const oldRequest = flow.request();
flow.switchAccount(2);
flow.loadResult(oldRequest.generation, oldRequest.epoch, { required: false, material_change: false });
check("account switch retires old policy load", flow.snapshot().status === null);
check("account switch rejects old submit before request", flow.submit(oldRequest.generation, oldRequest.epoch) === false);
const newRequest = flow.request();
flow.loadResult(newRequest.generation, newRequest.epoch, { required: true, material_change: true });
check("new account policy load is accepted", flow.snapshot().status?.material_change === true);
flow.conflict();
const afterConflict = flow.snapshot();
check("policy conflict clears old acknowledgement", afterConflict.status === null && afterConflict.age === null && afterConflict.guardian === false);

if (failures) process.exitCode = 1;
