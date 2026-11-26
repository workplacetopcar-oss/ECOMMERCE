const togglePassword = document.getElementById("toggle-password");
const password = document.getElementById("password");
togglePassword.hidden = false;
togglePassword.addEventListener("click", () => {
  const show = password.type === "password";
  password.type = show ? "text" : "password";
  togglePassword.textContent = show ? "Ocultar clave" : "Mostrar clave";
});
