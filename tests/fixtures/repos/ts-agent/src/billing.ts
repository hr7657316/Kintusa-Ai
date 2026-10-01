import Stripe from "stripe";

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!, { apiVersion: "2024-06-20" });

export async function charge(customerId: string, amount: number) {
  const intent = await stripe.paymentIntents.create({
    amount,
    currency: "usd",
    customer: customerId,
    confirm: true,
  });
  return intent.id;
}

export async function customer(id: string) {
  return stripe.customers.retrieve(id);
}

export async function recentCharges() {
  const list = await stripe.charges.list({ limit: 10 });
  return list.data;
}
