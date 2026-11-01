package com.example.checkboxes;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.widget.*;
import android.view.View;
public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        CheckBox cb1 = findViewById(R.id.cb1);
        CheckBox cb2 = findViewById(R.id.cb2);
        Button btn = findViewById(R.id.btn);

        btn.setOnClickListener(v -> {
            String s = "";
            if(cb1.isChecked()){
                s += cb1.getText();
            }
            if(cb2.isChecked()){
                s += cb2.getText();
            }
            if(s == ""){
                s = "Please select any checkbox";
            }
            Toast.makeText(this, s,Toast.LENGTH_LONG).show();

        });
    }
}