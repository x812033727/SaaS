(() => {
  const form = document.querySelector("#invoice-profile-form");
  if (!form) return;

  const mode = form.elements.mode;
  const carrierType = form.elements.carrier_type;
  const buyerName = form.elements.buyer_name;
  const buyerIdentifier = form.elements.buyer_identifier;
  const carrierNumber = form.elements.carrier_number;
  const donationCode = form.elements.donation_code;
  const businessSection = form.querySelector('[data-invoice-section="business"]');
  const carrierSection = form.querySelector('[data-invoice-section="carrier"]');
  const carrierNumberSection = form.querySelector('[data-invoice-section="carrier-number"]');
  const donationSection = form.querySelector('[data-invoice-section="donation"]');

  function syncSections() {
    const isBusiness = mode.value === "business";
    const isDonation = mode.value === "donation";
    const needsCarrierNumber = !isDonation && carrierType.value !== "ecpay";
    const hasStoredCarrier = carrierNumber.dataset.storedType === carrierType.value;

    businessSection.hidden = !isBusiness;
    carrierSection.hidden = isDonation;
    carrierNumberSection.hidden = !needsCarrierNumber;
    donationSection.hidden = !isDonation;

    buyerName.required = isBusiness;
    buyerIdentifier.required = isBusiness;
    carrierNumber.required = needsCarrierNumber && !hasStoredCarrier;
    donationCode.required = isDonation;
  }

  mode.addEventListener("change", syncSections);
  carrierType.addEventListener("change", syncSections);
  syncSections();
})();
